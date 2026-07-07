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
import os, sys, subprocess, time
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
import u1_review_doc
from u1_print_start_gate import build_stage1_command
from u1_slice_workflow import (
    _resolve_operator,
    _shell_quote,
    _real_upload,
    list_profiles,
    profile_path,
    apply_supports_override,
    apply_profile_overrides,
    _tool_to_index,
)

DEFAULT_TOOLS = ["T0", "T1", "T2", "T3"]
DEFAULT_MATERIALS = ["PLA", "PETG", "ABS", "TPU", "ASA", "PLA-CF", "PETG-CF"]
# Maps the form's supports vocabulary to the slice override vocabulary.
# "overhangs" was dropped from the offered options: enable_support is binary
# in the profile patch, so an overhangs-only mode was accepted + echoed but
# silently unimplemented. Re-add only alongside a real profile override.
_SUPPORTS_TO_OVERRIDE = {"supports": "supports", "no-supports": "no_supports",
                        "no_supports": "no_supports"}


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


# Set once per invocation (run_kit_workflow) to the EXPLICIT --operator CLI
# value, if any. _build_next_command stamps it into every emitted command so
# no call site can forget to thread it — the 2026-07-01 incident class was a
# smoke:* operator silently dropping to the production env default because
# a prompt's next_command omitted --operator. Env-resolved operators are
# deliberately NOT baked (replay-safe across operator config changes).
_CLI_OPERATOR: str | None = None


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
    if operator is None:
        operator = _CLI_OPERATOR
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


def _render_plate_isometric_from_gcode(
    gcode_path: Path,
    out_path: Path,
    *,
    bed_mm: tuple[float, float] = (270.0, 270.0),
    title: str | None = None,
    label_below: str | None = None,
    canvas_px: int = 1000,
) -> dict[str, Any]:
    """3D plate view built from the SAME sliced-gcode M486 outer walls as the
    top-down footprint, so the two views corroborate BY CONSTRUCTION.

    v2.2.1 fix: the previous isometric parsed Orca's ``--export-stl`` output,
    which uses a different (buggy) packer than ``--slice`` and produced a garbled
    overlapping layout that flatly disagreed with the footprint (live 2026-07-05).
    This reuses the proven M486 outer-wall extraction (same regex + arc handling
    the top-down uses), takes each part's mid-body outer boundary, extrudes it to
    its real Z height, and draws with an ELEVATED TOP-DOWN projection so the
    footprint's orientation is preserved (a rotating isometric mismatched the
    layout). Per-part colors match the top-down. Best-effort: returns ``ok:False``
    so the caller omits the 3D and still shows the footprint."""
    try:
        from PIL import Image, ImageDraw  # type: ignore
        import colorsys
        from collections import defaultdict
        import re
        import math
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": 0, "error": f"deps: {exc}"}
    NUM = r"(-?\d*\.?\d+)"
    G_RE = re.compile(r"^G[0123]\b"); G23_RE = re.compile(r"^G[23]\b")
    X_RE = re.compile(r"\bX" + NUM); Y_RE = re.compile(r"\bY" + NUM)
    Z_RE = re.compile(r"\bZ" + NUM); E_RE = re.compile(r"\bE" + NUM)
    I_RE = re.compile(r"\bI" + NUM); J_RE = re.compile(r"\bJ" + NUM)
    M486_A = re.compile(r"^M486 A(.+?)\s*$"); M486_S = re.compile(r"^M486 S(-?\d+)")
    try:
        lines = Path(gcode_path).read_text().splitlines()
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": 0, "error": f"gcode read: {exc}"}
    id_to_name: dict[int, str] = {}
    layers: dict[str, dict[float, list]] = defaultdict(lambda: defaultdict(list))
    heights: dict[str, float] = defaultdict(float)
    base_of: dict[str, str] = {}  # v2.2.2: instance name -> base model name (shared color)
    cid = None; cbase = None; ctype = None; prevx = prevy = None; cz = 0.0
    poly: list[tuple[float, float]] = []

    def flush() -> None:
        nonlocal poly
        if cbase and len(poly) >= 3:
            layers[cbase][round(cz, 2)].append(poly[:])
        poly = []

    for ln in lines:
        ma = M486_A.match(ln)
        if ma:
            if cid is not None:
                id_to_name[cid] = ma.group(1).strip()
            continue
        ms = M486_S.match(ln)
        if ms:
            flush(); nid = int(ms.group(1)); cid = None if nid < 0 else nid; cbase = None
            if cid is not None:
                nm = id_to_name.get(cid, "")
                # v2.2.2: key geometry by the FULL M486 instance name so two
                # copies of one model (same base, distinct _id_/_copy_) each keep
                # their own polygons and position. Keying by the stripped base
                # collapsed copies into one part in the 3D view (the largest-loop
                # pick below) while the top-down drew both, so the two review
                # images disagreed. Base name is kept only for a shared color.
                cbase = nm or None
                if cbase is not None:
                    base_of[cbase] = re.sub(r"_id_\d+_copy_\d+$", "", nm) or cbase
            continue
        zm = Z_RE.search(ln)
        if zm:
            cz = float(zm.group(1))
        if ln.startswith(";TYPE:"):
            flush(); ctype = ln[6:].strip(); continue
        if not G_RE.match(ln):
            continue
        xm = X_RE.search(ln); ym = Y_RE.search(ln); em = E_RE.search(ln)
        if not xm and not ym and not em:
            continue
        nx = float(xm.group(1)) if xm else prevx
        ny = float(ym.group(1)) if ym else prevy
        is_arc = bool(G23_RE.match(ln)); is_g2 = ln.startswith("G2")
        if em and cbase is not None and ctype == "Outer wall" and nx is not None and ny is not None:
            if float(em.group(1)) > 0 and prevx is not None:
                if not poly:
                    poly.append((prevx, prevy))
                if is_arc:
                    im = I_RE.search(ln); jm = J_RE.search(ln)
                    cx = prevx + (float(im.group(1)) if im else 0.0)
                    cyy = prevy + (float(jm.group(1)) if jm else 0.0)
                    r = math.hypot(prevx - cx, prevy - cyy)
                    sa = math.atan2(prevy - cyy, prevx - cx); ea = math.atan2(ny - cyy, nx - cx)
                    if is_g2:
                        if ea > sa: ea -= 2 * math.pi
                        sweep = sa - ea
                    else:
                        if ea < sa: ea += 2 * math.pi
                        sweep = ea - sa
                    nseg = max(2, int(abs(sweep) * r / 1.0))
                    for k in range(1, nseg + 1):
                        t = k / nseg; a = sa - sweep * t if is_g2 else sa + sweep * t
                        poly.append((cx + r * math.cos(a), cyy + r * math.sin(a)))
                else:
                    poly.append((nx, ny))
                heights[cbase] = max(heights[cbase], cz)
            else:
                flush()
        else:
            flush()
        prevx, prevy = nx, ny
    flush()

    if not layers:
        return {"ok": False, "path": None, "part_count": 0,
                "error": "no M486 outer-wall data (gcode missing per-part labels)"}

    def _area(lp) -> float:
        xs = [a for a, b in lp]; ys = [b for a, b in lp]
        return (max(xs) - min(xs)) * (max(ys) - min(ys))

    parts = sorted(layers)
    rep: dict[str, list] = {}
    for p in parts:
        zs = sorted(layers[p]); mid = zs[len(zs) // 2]
        rep[p] = max(layers[p][mid], key=_area)  # mid-body outer boundary

    E = math.radians(58)  # elevated top-down: preserves footprint orientation

    def proj(x, y, z):
        return (x, -(y * math.sin(E) + z * math.cos(E)))

    allp = []
    for p in parts:
        h = max(heights[p], 1.0)
        for (x, y) in rep[p]:
            allp.append(proj(x, y, 0)); allp.append(proj(x, y, h))
    ix = [a for a, _ in allp]; iy = [b for _, b in allp]
    if max(ix) == min(ix) or max(iy) == min(iy):
        return {"ok": False, "path": None, "part_count": len(parts), "error": "degenerate extent"}

    big_font, small_font = _load_pil_fonts()
    pad = 40; top_h = 84
    s = min(canvas_px / (max(ix) - min(ix)), (canvas_px * 0.82) / (max(iy) - min(iy)))

    def scr(pt):
        return (pad + (pt[0] - min(ix)) * s, top_h + pad + (pt[1] - min(iy)) * s)

    cw = int((max(ix) - min(ix)) * s) + 2 * pad
    ch = int((max(iy) - min(iy)) * s) + top_h + 2 * pad
    img = Image.new("RGB", (cw, ch), (22, 26, 31))
    d = ImageDraw.Draw(img, "RGBA")
    if title:
        d.text((pad, 18), title, fill=(232, 238, 245), font=big_font)
    sub = (f"{label_below}  -  " if label_below else "") + \
        "from the real sliced gcode (matches the footprint)"
    d.text((pad, 52), sub, fill=(140, 150, 160), font=small_font)
    n = len(parts)
    # v2.2.2: colour by base model, not by instance, so copies of one model
    # share a hue (and distinct models stay distinct) even though each instance
    # is now drawn separately.
    _bases = sorted({base_of.get(p, p) for p in parts})
    _base_idx = {b: k for k, b in enumerate(_bases)}
    _nb = len(_bases)

    def _depth(p):  # draw parts further back (higher y) first
        lp = rep[p]; return -sum(b for _, b in lp) / len(lp)

    for p in sorted(parts, key=_depth):
        r, g, b = colorsys.hsv_to_rgb(_base_idx[base_of.get(p, p)] / max(_nb, 1), 0.55, 0.9)
        bright = (int(r * 255), int(g * 255), int(b * 255))
        dim = tuple(int(c * 0.5) for c in bright)
        h = max(heights[p], 1.0); lp = rep[p]
        bot = [scr(proj(x, y, 0)) for x, y in lp]
        top = [scr(proj(x, y, h)) for x, y in lp]
        for k in range(len(lp) - 1):
            d.polygon([bot[k], bot[k + 1], top[k + 1], top[k]], fill=dim + (70,))
        d.line(bot + [bot[0]], fill=dim + (255,), width=2)
        d.line(top + [top[0]], fill=bright + (255,), width=3)

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, format="PNG", optimize=True)
    except Exception as exc:
        return {"ok": False, "path": None, "part_count": len(parts), "error": f"save: {exc}"}
    return {"ok": True, "path": str(out_path), "part_count": len(parts), "error": None}


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
    label_below = (f"{selected_count} part{'s' if selected_count != 1 else ''}"
                   f" • {tool_choice} {material}"
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
    # Dead-end renderers DELETED (rc2; recover via git history if ever
    # needed): _render_plate_layout_from_3mf (--export-3mf uses the same
    # buggy packer as --export-stl) and _render_plate_layout_from_gcode_m486
    # (M486 rotation matching under-constrained, visible overlaps).
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
    # Isometric 3D companion view built from the SAME sliced gcode as the
    # footprint above (v2.2.1: the old arranged-STL source used Orca's buggy
    # --export-stl packer and produced a garbled layout that disagreed with the
    # footprint). Corroborates by construction + shows height/orientation.
    # Best-effort: if it fails we skip it (the footprint still shows).
    iso = _render_plate_isometric_from_gcode(
        plate_gcode_path, out_dir / f"plate_{plate_idx}_iso.png",
        bed_mm=bed_mm,
        title=f"Plate {plate_idx} of {plate_count}  -  3D view",
        label_below=label_below)
    if iso.get("ok"):
        layout["iso_path"] = iso["path"]
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
                 "flagged as overhang-risk, 'yes' catches them."),
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
    # Merged head/material: read the live tool map so the head screen carries
    # each head's loaded filament + colour (and the separate Material screen is
    # dropped). Empty when no tool map is present — then the schema falls back
    # to generic T0–T3 + a Material screen (offline / tests).
    try:
        import u1_toolmap
        heads = u1_toolmap.load_head_options()
    except Exception:
        heads = []
    spec: dict[str, Any] = {
        "parts": parts,
        "tools": DEFAULT_TOOLS,
        "materials": DEFAULT_MATERIALS,
        "profiles": [{"idx": p["idx"], "label": p["label"]} for p in profiles_full],
        "supports": ["supports", "no-supports"],
        "actions": ["start", "upload-only"],
        "_prof_opts": [{"value": p["value"]} for p in profiles_full],  # idx -> resolution
        "_profiles_full": profiles_full,  # persisted at form-emit for index stability
        # v2.3: offer the optional Advanced screen (infill/pattern/walls/brim/
        # fuzzy skin) — reachable only from the form's Review button; the
        # default path never sees it.
        "offer_advanced": True,
    }
    if heads:
        spec["heads"] = heads
        spec["tool_materials"] = {h["tool"]: h["material"] for h in heads}
    return spec


_GATE_PREGRACE_WAIT = 25  # seconds to catch a fast refusal before detaching


_DOC_PREFIX_RE = re.compile(r"^doc_[0-9a-f]{8,}_")


def _strip_doc_prefix(stem: str) -> str:
    """Drop Hermes' document-cache prefix (``doc_<hash>_``) so the filename that
    lands on the printer LEADS with the model name, not the hash. Otherwise
    every plate shows as ``doc_55da642fda9e…`` in the printer's file list and the
    operator can only tell them apart by the thumbnail (operator 2026-07-04). The
    gcode hash is content-based (u1_request.compute_model_hash), so the rename
    does not affect request tracking/recovery."""
    stripped = _DOC_PREFIX_RE.sub("", str(stem))
    return stripped or str(stem)


def _invoke_stage2_gate(gate_py: str, argv: list[str], out_dir):
    """Launch the Stage-2 gate DETACHED and return its result, or None if it's
    still running (in the grace window).

    Why detached: the gate blocks up to ~120s for the pre-start grace/cancel
    window, but the agent's terminal tool call for --confirm-start times out at
    ~60s and kills its process tree — which killed the gate mid-grace and the
    print never started (live 2026-07-04). ``start_new_session=True`` puts the
    gate in its own process group so it survives the tool call returning.

    v2.2.1 #2: resolve the outcome via the child's EXPLICIT state marker, not by
    inferring 'still alive after 25s' == 'in the grace window'. Returns:
      - a result namespace (returncode, stdout) if the child EXITED (fast
        refusal / fast outcome),
      - None if the child wrote a ``grace_started`` marker (the ~120s window
        genuinely opened; leave it detached),
      - a namespace with ``stalled=True`` if the wait elapsed with the child
        still alive but NO grace marker (stuck in a pre-grace check / heading to
        a late refusal) — the caller must NOT report that as a healthy grace.
    Isolated so tests monkeypatch it (never contact Moonraker / block)."""
    from types import SimpleNamespace
    out_dir = Path(out_dir)
    # v2.2.2 #4: a unique id per launch so overlapping detached invocations for
    # the same request can't cross-talk on a shared marker. The child inherits it
    # via env and writes a run-scoped state file + log; the parent only ever polls
    # THIS run's marker (no stale-marker unlink needed — the path is unique).
    gate_run_id = os.urandom(5).hex()
    log_path = out_dir / f"stage2_gate_{gate_run_id}.log"
    state_path = out_dir / f"stage2_gate_state_{gate_run_id}.json"
    child_env = dict(os.environ, U1_GATE_RUN_ID=gate_run_id)
    logf = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, gate_py] + argv,
        stdout=logf, stderr=subprocess.STDOUT, start_new_session=True, env=child_env)

    def _finish(stalled=False):
        logf.close()
        try:
            out = log_path.read_text(errors="replace")
        except Exception:
            out = ""
        if stalled:
            return SimpleNamespace(stalled=True, returncode=None,
                                   stdout=out, stderr="")
        return SimpleNamespace(returncode=proc.returncode, stdout=out, stderr="")

    def _marker_state():
        try:
            return json.loads(state_path.read_text()).get("state")
        except Exception:
            return None

    waited, step = 0.0, 0.5
    while waited < _GATE_PREGRACE_WAIT:
        if proc.poll() is not None:
            return _finish()  # child exited -> real outcome in the log
        if _marker_state() == "grace_started":
            logf.close()
            return None  # grace window genuinely opened; leave it detached
        time.sleep(step)
        waited += step
    # Wait elapsed, child still alive, no grace marker: STALLED, not healthy.
    return _finish(stalled=True)


def run_kit_workflow(args) -> dict[str, Any]:
    """Orchestrate the kit path. See module docstring for the staged flow."""
    global _CLI_OPERATOR
    _CLI_OPERATOR = getattr(args, "operator", None) or None
    operator = _resolve_operator(args)

    # --confirm-start <token>: the operator said "yes" at the bed-clear prompt.
    # The model relays ONLY this short token (it mangled the old 200-char
    # verbatim command). Resolve the request + pending nonce from persisted
    # state, then fall through as a normal second-turn confirm (--action start
    # --bed-clear-confirmed). Single-use: the token is consumed on resolve.
    _confirm_token = getattr(args, "confirm_start", None)
    if _confirm_token:
        _rid = u1_form.resolve_confirm_token(_confirm_token)  # consumes it
        _state = u1_request.read_request(_rid) if _rid else None
        _pending = ((_state or {}).get("safety") or {}).get("pending_bed_clear_start") or {}
        if not (_rid and _state and _pending.get("nonce")):
            print(json.dumps({
                "stage": "bed_clear_confirm_token_invalid",
                "reason": ("confirm token is invalid, already used, or expired. "
                           "Re-run --action start (no --bed-clear-confirmed) to "
                           "get a fresh bed-clear prompt."),
            }), flush=True)
            return {"phase": "bed_clear_confirm_token_invalid",
                    "confirm_token": _confirm_token}
        args.request_id = _rid
        args.action = "start"
        args.bed_clear_confirmed = True
        args.pending_nonce = _pending.get("nonce")
        _audit(_rid, "bed_clear_confirm_token_redeemed", operator,
               token_first6=str(_confirm_token)[:6])
        if _state.get("reprint_of"):
            # Reprint confirm (v2.3): there is NO archive to re-ingest — the
            # gcode is already in printer storage and everything the confirmed
            # turn validates (pending nonce, revision, plate hash, tool,
            # material) is persisted on the request. Falling through would hit
            # the model-positional recovery and die on the original upload's
            # long-gone cache file (live 2026-07-06: the operator's YES
            # errored, stranding the reprint at the finish line). Route
            # straight to the gate turn.
            return _action_start(None, _rid,
                                 bool(getattr(args, "json_events", False)),
                                 bed_clear_confirmed=True, operator=operator,
                                 pending_nonce=_pending.get("nonce"))
    # ── REPRINT (v2.3): both turns work with NO model positional ──
    # Turn 1 (--reprint): list recent prints, one single-use pick token each.
    # Turn 2 (--reprint-start <token>): seed a fresh request from the picked
    # print and jump straight to the bed-clear boundary — no slicing at all.
    if getattr(args, "reprint_start", None):
        return _action_reprint_start(None, bool(getattr(args, "json_events", False)),
                                     operator, args.reprint_start)
    if getattr(args, "reprint", False):
        return _action_reprint_list(None, bool(getattr(args, "json_events", False)),
                                    operator)

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
    # Ingest failures (over-limit archive, unparseable/garbage STL, corrupt
    # zip) are operator problems, not crashes: emit a clean kit_rejected
    # event instead of dying with a traceback.
    parts_dir = out_dir / "parts"
    try:
        stls = u1_kit.extract_all_stls(archive, parts_dir)
        kit = u1_kit.build_kit(stls)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _emit(events_file, {
            "stage": "kit_rejected",
            "request_id": request_id,
            "error": reason[:600],
            "instruction": ("This archive could not be ingested as a kit "
                            "(bad/oversized entry or unparseable STL). "
                            "Surface the error to the operator; do not "
                            "retry with the same file."),
        }, json_events)
        _audit(request_id, "kit_rejected", operator, error=reason[:300])
        return {"phase": "kit_rejected", "request_id": request_id,
                "error": reason[:600]}
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
    _kit_payload: dict[str, Any] = {"parts": kit["parts"],
                                    "part_count": kit["part_count"]}
    if _action_now and _action_now in _post_confirm_actions:
        # write_request replaces `kit` wholesale — preserve the confirm's
        # selected/orient_mode keys or the post-confirm backfill below
        # (and the refresh handler's state guard) loses its inputs.
        _existing_kit = (u1_request.read_request(request_id) or {}).get("kit") or {}
        for _k in ("selected", "orient_mode"):
            if _k in _existing_kit:
                _kit_payload[_k] = _existing_kit[_k]
    _persist_kwargs = dict(
        model_file=archive.name, model_path=str(archive),
        model_hash=u1_request.compute_model_hash(archive) if archive.exists() else None,
        out_dir=str(out_dir), operator=operator,
        kit=_kit_payload,
    )
    if not _action_now or _action_now not in _post_confirm_actions:
        _persist_kwargs["phase"] = "kit_analysis"
    u1_request.write_request(request_id, **_persist_kwargs)

    # Post-confirm resume: a post-confirm action with missing turn flags
    # must NOT fall back into the staged Q&A — that path re-slices and
    # re-uploads, the exact "hand-built command fell back to Turn 1" bug.
    # Backfill missing answers from the persisted confirm state (explicit
    # CLI flags always win; downstream guards still validate drift).
    if _action_now and _action_now in _post_confirm_actions:
        _state = u1_request.read_request(request_id) or {}
        _kit_state = _state.get("kit") or {}
        _backfilled: list[str] = []
        if not getattr(args, "parts", None):
            _sel = _kit_state.get("selected") or []
            _id2idx = {p["part_id"]: i + 1 for i, p in enumerate(kit["parts"])}
            _idxs = [str(_id2idx[pid]) for pid in _sel if pid in _id2idx]
            if _idxs:
                args.parts = ("all" if len(_idxs) == kit["part_count"]
                              else ",".join(_idxs))
                _backfilled.append("parts")
        for _field, _persisted_val in (
                ("orient", _kit_state.get("orient_mode")),
                ("tool", _state.get("tool")),
                ("material", _state.get("material")),
                ("profile", _state.get("profile")),
                ("supports", _state.get("supports"))):
            if not getattr(args, _field, None) and _persisted_val:
                setattr(args, _field, _persisted_val)
                _backfilled.append(_field)
        if _backfilled:
            _audit(request_id, "post_confirm_flags_backfilled", operator,
                   action=_action_now, fields=_backfilled)

    # ── LEGACY: one-liner power-user path (CLI tests + scripted runs) ──
    answers = getattr(args, "form_answers", None)
    answers_json = getattr(args, "form_answers_json", None)
    answers_from = getattr(args, "form_answers_from", None)
    # v2.2.2: idempotency for a DUPLICATE form-redeem. If the request already
    # advanced to the bed-clear step (the first redeem sliced + uploaded and is
    # awaiting the operator's yes), a second --redeem-pending-form must NOT
    # re-render a fresh form — that stranded the operator in a form loop when a
    # small model relayed the redeem command twice (live 2026-07-06). Re-surface
    # the SAME bed-clear prompt (same still-valid confirm token) instead, so the
    # duplicate relay is a harmless no-op.
    if (getattr(args, "redeem_pending_form", False)
            and not getattr(args, "action", None)):
        # Detect the already-advanced state by the DURABLE pending object, NOT
        # the phase: the looping re-form reset phase to awaiting_form while the
        # pending_bed_clear_start (nonce + confirm token) survived in safety
        # (confirmed on the live 2026-07-06 request).
        _idem_req = u1_request.read_request(request_id) or {}
        _idem_safety = _idem_req.get("safety") or {}
        _idem_pending = _idem_safety.get("pending_bed_clear_start") or {}
        _idem_tok = _idem_pending.get("confirm_token")
        # Fire ONLY for a genuine DUPLICATE redeem — the current form's answers
        # are already consumed. A FIRST redeem still has its answers file and must
        # proceed normally (slice + overwrite any stale pending); otherwise a
        # request carrying a stale pending from a PRIOR run (request ids are
        # content-derived, so re-uploads reuse the request) would wrongly
        # re-surface the OLD plate instead of slicing the fresh answers.
        _idem_fid = _idem_req.get("form_id")
        try:
            _answers_gone = not (_idem_fid and u1_form._answers_path(_idem_fid).exists())
        except Exception:
            _answers_gone = True
        if _idem_pending.get("nonce") and _idem_tok and _answers_gone:
            _idem_prompt = (
                "You already submitted this plate — it's sliced, uploaded, and "
                "waiting on your bed-clear yes. Reply YES to start now, or NO to "
                "keep the gcode staged without printing.")
            _emit(events_file, {
                "stage": "need_input", "request_id": request_id,
                "need": "bed_clear_start", "key": "bed_clear_start",
                "requires_fresh_operator_bed_clear": True,
                "approval_prompt_key": "bed_clear_start",
                "prompt": _idem_prompt,
                "instruction": ("The plate preview + bed photo were already sent "
                                "on the previous turn — do NOT re-run the form. "
                                "Re-surface the yes/no prompt and wait."),
                "expected_answers": ["yes", "no"],
                "next_command_on_yes": (
                    f"python3 /opt/data/scripts/u1_kit_workflow.py "
                    f"--confirm-start {_idem_tok}"),
                "next_command_on_no": None,
                "bed_snapshot_path": (_idem_safety.get("snapshot_path")
                                      or _idem_safety.get("bed_snapshot_path")),
            }, json_events)
            _emit(events_file, {"stage": "awaiting_input",
                                "need": "bed_clear_start",
                                "request_id": request_id}, json_events)
            _audit(request_id, "duplicate_redeem_reemitted_bed_clear", operator)
            return {"phase": "awaiting_bed_clear_start",
                    "request_id": request_id, "prompt": _idem_prompt}

    if getattr(args, "redeem_pending_form", False) and not answers_from:
        # The model relays NO form_id — it derives from the request, which
        # persisted form_id at emit time. gemma4 mangled the random-hex form_id
        # in the verbatim redeem command (live 2026-07-03: f7b273e3536 →
        # f7b273e3504 → "form id mismatch"), the same failure mode the
        # confirm-start short token fixed. Deriving it kills the manglable token.
        answers_from = (u1_request.read_request(request_id) or {}).get("form_id")
    if answers or answers_json or answers_from:
        return _run_legacy_form_answers(
            args, operator, archive, kit, request_id, out_dir, events_file,
            json_events, answers, answers_json, answers_from)

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

    # ── FORM MODE (v2.2): one consolidated form instead of staged turns ──
    # The form_schema is rendered by an adapter (buttons); the GATEWAY
    # writes the collected answers to a file keyed by form_id and the
    # agent redeems it by relaying next_command verbatim. No answer
    # content ever passes through the model — same trust level as the
    # --pending-nonce contract. Text fallback stays available.
    if interaction_mode == "form" and not getattr(args, "action", None):
        _existing = u1_request.read_request(request_id) or {}
        spec = _build_form_spec(kit, nozzle,
                                persisted_profiles=_existing.get("form_profiles"))
        # Kit-of-1 (a lone STL routed through the unified flow): run the
        # single-model orientation verdict so the orient button is data-driven —
        # recommend the pose Orca actually prefers, with the reason. Proven to
        # earn its cost (2026-07-03: catches floating regions as-authored →
        # recommends auto). Cheap: one model, and orient_verdict only draft-
        # slices the SECOND pose when the first has overhangs. Computed once and
        # persisted so an idempotent re-emit reuses it. Skipped for true
        # multi-part kits (draft-slicing every part is too expensive; they
        # already auto-orient at slice time).
        if len(kit.get("parts", [])) == 1:
            _ov = _existing.get("orient_verdict")
            if _ov is None:
                _ov = {}
                try:
                    import u1_slice_workflow as _sw
                    _pv = (spec.get("_prof_opts") or [{}])[0].get("value")
                    if _pv:
                        _od = (u1_request.ensure_request_dir(request_id)
                               / "orient_analysis")
                        _od.mkdir(parents=True, exist_ok=True)
                        _res = _sw.orient_verdict(
                            Path(kit["parts"][0]["path"]), _od,
                            Path(_sw.profile_path(_pv)),
                            _sw.filament_path(DEFAULT_MATERIALS[0], nozzle=nozzle))
                        if _res.get("ok"):
                            _ov = {"recommendation": _res["recommendation"],
                                   "note": _res.get("note")}
                except Exception:
                    _ov = {}  # verdict is a nicety; never block the form on it
                u1_request.write_request(request_id, orient_verdict=_ov)
            if _ov:
                spec["orient_recommendation"] = _ov.get("recommendation")
                spec["orient_note"] = _ov.get("note")
        if not spec["profiles"]:
            _emit(events_file, {"stage": "setup_required", "kind": "no_profiles",
                                "message": ("No profiles found. Run "
                                            "tools/fetch_snapmaker_profiles.py.")},
                  json_events)
            return {"phase": "setup_required", "request_id": request_id,
                    "out_dir": str(out_dir)}
        # Re-invocation while a form is pending re-emits the SAME form_id —
        # idempotent re-prompt, no orphaned answer files. (A successful
        # commit clears form_id, so a stale one can't leak into a new run;
        # the phase itself is unreliable here because the bare-reinvoke
        # guard upstream resets it to kit_analysis by design.)
        form_id = _existing.get("form_id") or u1_form.new_form_id()
        u1_request.write_request(request_id, phase="awaiting_form",
                                 form_id=form_id,
                                 form_profiles=spec["_profiles_full"])
        # Render the per-part STL thumbnail grid BEFORE the form so the
        # operator can see each piece while picking parts — the staged flow
        # does this at Turn 1; form mode was skipping it (operator feedback
        # 2026-07-02: "I didn't get a photo of the models before selecting").
        _thumb = _render_parts_thumbnail_grid(
            kit, u1_request.ensure_request_dir(request_id) / "parts_thumbnails.png")
        if _thumb.get("ok"):
            _emit(events_file, {"stage": "render", "request_id": request_id,
                                "kind": "parts_thumbnail_grid",
                                "image": _thumb["path"]}, json_events)
        schema = u1_form.build_form_schema(
            spec, submit={"mode": "file", "form_id": form_id})
        # Carry the thumbnail path IN the schema so the form renderer sends it
        # as a photo with the first screen — the operator sees the pieces
        # while picking, without relying on the agent to surface the render
        # (operator feedback 2026-07-02: picked parts with no photo).
        if _thumb.get("ok"):
            schema["header_image"] = _thumb["path"]
        # Persist the schema to disk; the agent relays ONLY the flat form_id.
        # A 26B local model (gemma4) could not reproduce the nested schema in
        # a tool call — it emitted template-token soup as plain text
        # (finish=stop, no tool_calls; Ollama #15539/#15798/#15943) and the
        # flow stranded. Verified 2026-07-03: flat form_id call succeeds where
        # the nested-schema call failed (19s vs 819s, temp-0.2 variant). The
        # event is also kept SLIM (no schema/text_fallback) so the model
        # never sees nested JSON it might try to echo.
        u1_form.persist_schema(form_id, schema)
        # Redeem WITHOUT relaying the form_id — the model mangled the random-hex
        # id in the verbatim command (gemma4, live 2026-07-03). --redeem-pending-
        # form makes the workflow read form_id off the request instead.
        redeem_cmd = _build_next_command(
            archive, request_id, nozzle=nozzle,
            no_live_upload=no_live_upload,
            no_live_material=no_live_material) + " --redeem-pending-form"
        if not no_live_upload:
            redeem_cmd += " --live-upload"
        _emit(events_file, {
            "stage": "need_input", "key": "kit_form",
            "request_id": request_id,
            "form_id": form_id,
            "next_command": redeem_cmd,
            "instruction": (
                f"Your VERY NEXT action is a single tool call: "
                f"form(form_id=\"{form_id}\"). Nothing else — no text first, "
                "no image paths, no restated options (the form renderer "
                "shows the parts thumbnail and every choice as buttons). "
                "When the gateway confirms the answers file is written, "
                "tool-call next_command VERBATIM — do not add, remove, or "
                "restate any answer. If the form tool errors, is "
                "unavailable, or times out: re-run THIS SAME kit command "
                "with --interaction-mode text appended — that starts the "
                "staged one-question-per-turn flow."),
        }, json_events)
        _emit(events_file, {"stage": "awaiting_input", "need": "kit_form",
                            "request_id": request_id}, json_events)
        _audit(request_id, "kit_form_emitted", operator, form_id=form_id,
               mode="form")
        return {"phase": "awaiting_form", "request_id": request_id,
                "form_id": form_id}

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
    # Operator is NOT passed here: _build_next_command stamps the explicit
    # CLI operator (_CLI_OPERATOR) into every emitted command uniformly, and
    # env-resolved identity stays env-resolved (replay-safe).
    _yes_base = _build_next_command(
        archive, request_id, parts=parts_answer, tool=tool_choice,
        material=material, orient=orient, profile=profile_slug,
        supports=supports, action=action, nozzle=nozzle,
        no_live_upload=no_live_upload, no_live_material=no_live_material)
    yes_command_on_confirmed = _yes_base + " --bed-clear-confirmed"

    # Action handlers
    if action == "start":
        return _action_start(events_file, request_id, json_events,
                             yes_command=yes_command_on_confirmed,
                             bed_clear_confirmed=bool(
                                 getattr(args, "bed_clear_confirmed", False)),
                             operator=operator,
                             pending_nonce=getattr(args, "pending_nonce", None))
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
                getattr(args, "bed_clear_confirmed", False)),
            pending_nonce=getattr(args, "pending_nonce", None))
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
    kit_stem = u1_kit._sanitize(_strip_doc_prefix(archive.stem))
    # Plate filenames are deterministic ({kit_stem}_plateN.gcode), so an
    # adjust → re-confirm on the SAME request always collides with its own
    # earlier upload — rc=5 dead-ended the advertised adjust option. When
    # THIS request already uploaded a given filename, overwriting it is
    # re-uploading our own plate; collisions with anything else still ask.
    _own_prior_names: set[str] = set()
    try:
        _prev_req = u1_request.read_request(request_id) or {}
        for _p in (_prev_req.get("plates") or []):
            _n = _p.get("printer_storage_filename")
            if _n:
                _own_prior_names.add(_n)
    except Exception:
        pass
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
        _oc = getattr(args, "on_collision", None)
        if _oc is None:
            # Re-uploading THIS request's own prior name (adjust -> re-confirm)
            # overwrites it. ANY OTHER collision -> rename with a timestamp
            # suffix so the upload never fails and never clobbers a different
            # job's file. This is the common case now that doc-hash filename
            # prefixes are dropped (82a9681): re-printing a model a PRIOR
            # request already uploaded = same base name. rename appends the
            # timestamp ONLY on collision, so the first print keeps the clean
            # name and the model name still leads (printer-list readability).
            _oc = "overwrite" if named.name in _own_prior_names else "rename"
        up = (_real_upload(named,
                            on_collision=_oc,
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
            "iso_path": layout.get("iso_path"),
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
        # Keep the two hash-binding surfaces consistent: plates[0].gcode_hash
        # (nonce binds) and top-level gcode_hash (can_start) must not point
        # at different slices. Sync the top-level binding to the new plate 1
        # and clear any approval/nonce from a PRIOR confirm — that yes was
        # for a plan whose upload succeeded, not this one. (write_request
        # replaces `safety` wholesale, so merge over the current block.)
        _cur_safety = dict(((u1_request.read_request(request_id) or {})
                            .get("safety") or {}))
        _cur_safety.update({"approval_token": None,
                            "stage2_approval_nonce": None,
                            "stage2_approval_binds": None})
        u1_request.write_request(request_id, phase="upload_failed",
                                 plates=plates_state,
                                 gcode_hash=(plates_state[0]["gcode_hash"]
                                             if plates_state else None),
                                 safety=_cur_safety)
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

    # Pre-print review doc (v2.2): the operator's flight plan, generated
    # from the sliced gcode's own config block. Strictly fail-soft — a doc
    # problem must never block a print.
    review_doc_path: str | None = None
    try:
        review_doc_path = str(u1_review_doc.generate(
            request_id, out_dir, plates_state,
            state=u1_request.read_request(request_id) or {},
            decisions={"tool": tool_choice, "material": material,
                       "profile": profile_slug, "orient": orient,
                       "supports": supports,
                       "parts": ", ".join(p["part_id"] for p in selected)},
            overrides=([f"supports forced {'ON' if supports == 'supports' else 'OFF'} "
                        f"by your answer (preset value overridden)"]
                       if supports in ("supports", "no_supports") else None),
            operator=operator,
            reference=u1_review_doc.build_reference(
                profile_slug, material, nozzle=nozzle, out_dir=out_dir),
            envelope=u1_review_doc.build_material_envelope(
                material, nozzle=nozzle, out_dir=out_dir),
        ))
        _emit(events_file, {
            "stage": "review_doc", "request_id": request_id,
            "path": review_doc_path,
            "instruction": ("ATTACH this file (surface the bare path so Hermes "
                            "sends it as a document) alongside the plan card — do "
                            "NOT paste its contents inline; it is markdown with "
                            "tables that render as a wall of pipes in a chat "
                            "message. It is the human-readable review of exactly "
                            "what will print."),
        }, json_events)
    except Exception as _rd_exc:
        _audit(request_id, "review_doc_failed", operator,
               error=f"{type(_rd_exc).__name__}: {_rd_exc}"[:200])

    # Readiness card carries the plan + captured photo/status. The raw
    # Stage 2 command and approval token are NOT surfaced here's
    # 2026-07-01 audit flagged that as a shortcut path any adapter could
    # grab to bypass the bed_clear_start yes/no turn. The token stays in
    # persisted request state (safety.approval_token) where only the
    # workflow's _action_start() can reach it.
    readiness = {
        "stage": "kit_readiness_card",
        "review_doc_path": review_doc_path,
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
        # start_gate_stage1_command is deliberately NOT surfaced here anymore.
        # It's a runnable gate command, and a confused agent grabbed it and ran
        # the gate directly — Stage 1 then a Stage 2 with no nonce, which the
        # gate correctly refused, costing the operator a scary turn (live
        # 2026-07-02, twice). The ONLY start path is the `start` option's
        # next_command (--action start), which drives the bed-clear yes/no and
        # emits the nonce-bound Stage 2 command itself. The command is still
        # persisted in request state (write_request below) for _action_start's
        # own no-token fallback.
        "operator_guidance": (
            f"{len(plates_state)} plate(s). Plate 1 ({plate1['printer_storage_filename']}) is "
            f"start-gated. Plates 2..{len(plates_state)} are already uploaded; "
            "start them from the Snapmaker app after plate 1 finishes."
            if len(plates_state) > 1 else
            "Single plate. Slice & review, then a fresh bed-clear yes/no starts the print."
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
            "label": "Slice & review — then a fresh bed-clear yes/no starts the print",
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
    if printer_busy:
        # Busy printer: the plates are uploaded but no bed photo was taken.
        # Offer the refresh path so the operator can gate plate 1 once the
        # printer idles — without this option the documented busy→idle
        # recovery flow was unreachable (agents copy commands verbatim).
        options.append({
            "label": ("Refresh bed photo — once the printer is idle, "
                      "re-capture the bed and continue to start (no "
                      "re-slice)"),
            "value": "refresh-bed-photo",
            "next_command": _build_next_command(
                archive, request_id, parts=parts_answer, tool=tool_choice,
                material=material, orient=orient, profile=profile_slug,
                supports=supports, action="refresh-bed-photo", nozzle=nozzle,
                no_live_upload=no_live_upload, no_live_material=no_live_material),
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
        if preview_result.get("iso_path"):
            _emit(events_file, {"stage": "render", "request_id": request_id,
                                "kind": "kit_plate_isometric",
                                "image": preview_result["iso_path"]}, json_events)
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
                        "operator's reply (`start` / `upload-only` / `adjust`). "
                        "When the operator answers, tool-call the CHOSEN option's "
                        "next_command VERBATIM. NEVER run u1_print_start_gate.py "
                        "or start_gate_stage1_command yourself — the workflow drives "
                        "the bed-clear yes/no turn and emits the Stage 2 command for "
                        "you; running the gate directly skips the nonce and the gate "
                        "will refuse."),
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


def _printer_gcode_filenames() -> set[str] | None:
    """Names in the printer's gcodes root, or None when Moonraker is
    unreachable (callers treat None as unknown and fail closed at start)."""
    import urllib.request
    try:
        from u1_config import get_u1_host, get_u1_port
        url = (f"http://{get_u1_host()}:{get_u1_port()}"
               "/server/files/list?root=gcodes")
        with urllib.request.urlopen(url, timeout=8) as r:
            payload = json.loads(r.read().decode())
        return {str(f.get("path", "")) for f in payload.get("result", [])}
    except Exception:
        return None


def _reprint_candidates(limit: int = 6) -> list[dict[str, Any]]:
    """Recent reprintable jobs: requests that uploaded a plate-1 gcode,
    newest first, deduped by printer filename, enriched with the print-history
    record (state/duration) and an on-printer check."""
    from u1_config import get_data_dir
    root = Path(get_data_dir()) / "requests"
    if not root.is_dir():
        return []
    # History join: printer filename -> latest ledger record.
    history: dict[str, dict[str, Any]] = {}
    try:
        hist = json.loads((Path(get_data_dir()) / "print_history.json").read_text())
        for rec in hist.get("records", []):
            if rec.get("filename"):
                history[rec["filename"]] = rec
    except Exception:
        pass
    on_printer = _printer_gcode_filenames()

    dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)[:60]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in dirs:
        try:
            req = json.loads((d / "request.json").read_text())
        except Exception:
            continue
        fname = req.get("printer_storage_filename")
        plates = req.get("plates") or []
        if not (fname and plates and plates[0].get("gcode_hash")):
            continue  # never uploaded — nothing to reprint
        if fname in seen:
            continue
        seen.add(fname)
        rec = history.get(fname) or {}
        est = rec.get("estimated_time_s")
        dur = (f"{int(est // 3600)}h {int(est % 3600 // 60)}m" if est and est >= 3600
               else f"{int(est // 60)}m" if est else None)
        # Human name: strip the Hermes doc-cache prefix (doc_<hash>_), the
        # same cleanup printer filenames get.
        model = re.sub(r"^doc_[0-9a-f]{6,}_",
                       "", Path(req.get("model_file") or fname).stem)
        # Same model re-uploaded under a different cache hash reads as an
        # identical option — keep only the newest per (model, tool, material).
        model_key = f"{model}|{req.get('tool')}|{req.get('material')}"
        if model_key in seen:
            continue
        seen.add(model_key)
        out.append({
            "request_id": req.get("request_id") or d.name,
            "printer_storage_filename": fname,
            "model": model,
            "tool": req.get("tool"),
            "material": req.get("material"),
            "printed_state": rec.get("state"),
            "duration": dur,
            "last_seen_at": rec.get("last_seen_at"),
            "on_printer": (None if on_printer is None else fname in on_printer),
        })
        if len(out) >= limit:
            break
    return out


def _action_reprint_list(events_file: Path | None, json_events: bool,
                         operator: str) -> dict[str, Any]:
    """Turn 1: list recent prints, each with a single-use pick token. The
    model relays ONE short token command verbatim — same mangle-proof shape
    as --confirm-start."""
    cands = _reprint_candidates()
    if not cands:
        _emit(events_file, {
            "stage": "reprint_none_available",
            "message": ("No reprintable jobs found — nothing in the request "
                        "history has an uploaded plate. Send a model file to "
                        "start a fresh print.")}, json_events)
        return {"phase": "reprint_none_available"}
    options = []
    for i, c in enumerate(cands, 1):
        tok = u1_form.new_confirm_token()
        u1_form.persist_confirm_token(tok, c["request_id"])
        bits = [c["model"]]
        if c.get("material") or c.get("tool"):
            bits.append(f"{c.get('material') or '?'} on {c.get('tool') or '?'}")
        if c.get("printed_state") == "complete":
            bits.append("printed" + (f", ~{c['duration']}" if c.get("duration") else ""))
        elif c.get("printed_state"):
            bits.append(str(c["printed_state"]))
        else:
            bits.append("uploaded, not printed")
        label = " — ".join(bits)
        if c.get("on_printer") is False:
            label += " (no longer on printer)"
        options.append({
            "n": i, "label": label,
            "next_command": (f"python3 /opt/data/scripts/u1_kit_workflow.py "
                             f"--reprint-start {tok}"),
        })
    _emit(events_file, {
        "stage": "need_input", "need": "reprint_pick", "key": "reprint_pick",
        "prompt": "Which print do you want to run again?",
        "options": options,
        "instruction": ("Surface the options to the operator as a numbered "
                        "list (labels only). Wait for their pick, then "
                        "tool-call THAT option's next_command VERBATIM. Do "
                        "not invent filenames or edit the token."),
    }, json_events)
    _emit(events_file, {"stage": "awaiting_input", "need": "reprint_pick"},
          json_events)
    return {"phase": "awaiting_reprint_pick", "options_count": len(options)}


def _action_reprint_start(events_file: Path | None, json_events: bool,
                          operator: str, token: str) -> dict[str, Any]:
    """Turn 2: seed a fresh request from the picked print and enter the
    standard bed-clear boundary. No slicing: the gcode is already in printer
    storage; the gate still re-verifies material live, validates the file
    exists before grace, and runs the full nonce/grace/cancel chain."""
    old_rid = u1_form.resolve_confirm_token(token)  # single-use, atomic claim
    old = u1_request.read_request(old_rid) if old_rid else None
    if not (old_rid and old):
        _emit(events_file, {
            "stage": "reprint_token_invalid",
            "reason": ("pick token is invalid, already used, or expired — "
                       "run --reprint again for a fresh list.")}, json_events)
        return {"phase": "reprint_token_invalid"}
    plates = old.get("plates") or []
    fname = old.get("printer_storage_filename") or (
        plates[0].get("printer_storage_filename") if plates else None)
    if not (fname and plates and plates[0].get("gcode_hash")):
        _emit(events_file, {
            "stage": "reprint_unavailable", "request_id": old_rid,
            "reason": "that request never uploaded a plate — send the model "
                      "file for a fresh slice instead."}, json_events)
        return {"phase": "reprint_unavailable", "request_id": old_rid}

    # Hard existence check now (better UX than failing at the gate; the gate
    # still re-validates pre-grace as defense in depth).
    names = _printer_gcode_filenames()
    if names is None or fname not in names:
        reason = ("printer unreachable — cannot verify the file is still on "
                  "the printer" if names is None else
                  f"'{fname}' is no longer in printer storage")
        _emit(events_file, {
            "stage": "reprint_file_missing", "request_id": old_rid,
            "reason": reason,
            "instruction": ("Tell the operator the reprint can't start and "
                            "to re-send the original model file for a fresh "
                            "slice.")}, json_events)
        return {"phase": "reprint_file_missing", "request_id": old_rid}

    new_rid = u1_request.generate_request_id()
    new_dir = Path(u1_request.ensure_request_dir(new_rid))
    bed = _capture_bed_and_issue_token(new_dir)
    if not bed.get("ok"):
        _emit(events_file, {
            "stage": "reprint_bed_capture_failed", "request_id": new_rid,
            "reason": bed.get("reason"),
            "instruction": ("Bed photo failed — the reprint fails closed. "
                            "Tell the operator why and stop.")}, json_events)
        _audit(new_rid, "reprint_bed_capture_failed", operator,
               reprint_of=old_rid, reason=str(bed.get("reason"))[:160])
        return {"phase": "reprint_bed_capture_failed", "request_id": new_rid}

    u1_request.write_request(
        new_rid,
        reprint_of=old_rid,
        model_file=old.get("model_file"),
        tool=old.get("tool", "T0"),
        material=old.get("material", "PETG"),
        request_revision=1,
        printer_storage_filename=fname,
        # Top-level gcode_hash is what can_start() drift-checks against the
        # audited readiness row.
        gcode_hash=plates[0].get("gcode_hash"),
        plates=[{"plate_idx": 1,
                 "gcode_hash": plates[0].get("gcode_hash"),
                 "gcode_path": plates[0].get("gcode_path"),
                 "printer_storage_filename": fname,
                 "uploaded": True}],
        operator=operator,
        safety={"approval_token": bed["token"],
                "snapshot_path": bed.get("snapshot_path"),
                "bed_clear_photo_captured": True,
                "bed_clear_check_required": True},
    )
    _audit(new_rid, "reprint_initiated", operator, reprint_of=old_rid,
           printer_filename=fname)

    # Re-surface the ORIGINAL previews + review doc (they live in the old
    # request dir), then the fresh bed photo — so the stock bed-clear prompt
    # ("sliced plate, review doc, and a fresh bed photo are attached") stays
    # literally true for a reprint.
    for kind, path in (("kit_plate_preview", plates[0].get("preview_path")),
                       ("kit_plate_isometric", plates[0].get("iso_path"))):
        if path and Path(path).is_file():
            _emit(events_file, {"stage": "render", "request_id": new_rid,
                                "kind": kind, "image": path,
                                "instruction": "Surface this image path BARE in your reply."},
                  json_events)
    _old_review = Path(old.get("out_dir") or "") / "review.md"
    if _old_review.is_file():
        _emit(events_file, {"stage": "review_doc", "request_id": new_rid,
                            "path": str(_old_review)}, json_events)
    if bed.get("snapshot_path"):
        _emit(events_file, {"stage": "render", "request_id": new_rid,
                            "kind": "bed_snapshot", "image": bed["snapshot_path"],
                            "instruction": "Surface this fresh bed photo path BARE in your reply."},
              json_events)

    # This turn IS the operator's review moment (original previews + review
    # doc + fresh bed photo, above). Record it with the same revision+hash
    # binding a kit readiness card carries — can_start() drift-checks the
    # start against exactly this row.
    _audit(new_rid, "reprint_readiness_card_emitted", operator,
           request_revision=1, gcode_hash=plates[0].get("gcode_hash"),
           reprint_of=old_rid, printer_filename=fname)

    return _action_start(events_file, new_rid, json_events,
                         bed_clear_confirmed=False, operator=operator)


def _action_start(events_file: Path | None, request_id: str,
                  json_events: bool,
                  yes_command: str | None = None,
                  bed_clear_confirmed: bool = False,
                  operator: str | None = None,
                  pending_nonce: str | None = None) -> dict[str, Any]:
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
    if not token:
        # Legacy-path loop-closer: when the confirm never persisted a token
        # (bed capture failed → operator ran the emitted Stage 1 command),
        # Stage 1 wrote its token to the request-dir sidecar only. Without
        # adopting it here, --action start re-emitted the Stage 1 command
        # forever: Stage 1 → start → Stage 1 → … Fail-closed still holds —
        # the gate re-validates token TTL + photo binding at Stage 2.
        try:
            from u1_print_start_gate import _read_approval_token
            _sidecar = _read_approval_token(u1_request.request_dir(request_id))
            if _sidecar and _sidecar.get("token"):
                token = _sidecar["token"]
                safety = dict(safety)
                safety["approval_token"] = token
                safety.setdefault("bed_clear_photo_captured", True)
                u1_request.write_request(request_id, safety=safety)
                _audit(request_id, "stage1_token_adopted_from_sidecar",
                       operator, token_first8=token[:8])
        except Exception:
            pass
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
        # Short confirm token the model relays instead of the full command
        # (gemma4 mangled the long verbatim relay — see u1_form confirm-token
        # note). Maps to this request; single-use; the nonce still does the
        # real auth.
        confirm_token = u1_form.new_confirm_token()
        pending = {
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "request_revision": request_revision,
            "gcode_hash": gcode_hash,
            "prompt_key": "bed_clear_start",
            "nonce": nonce,
            "confirm_token": confirm_token,
        }
        # Persist the pending approval into safety block, preserving
        # existing safety fields (token/snapshot_path/etc).
        new_safety = dict(safety)
        new_safety["pending_bed_clear_start"] = pending
        u1_request.write_request(request_id,
                                 phase="awaiting_bed_clear_start",
                                 safety=new_safety)
        try:
            u1_form.persist_confirm_token(confirm_token, request_id)
        except Exception as _ct_exc:
            _audit(request_id, "confirm_token_persist_failed", operator,
                   error=f"{type(_ct_exc).__name__}: {_ct_exc}"[:160])
        # Reference the photo the operator already saw with the print
        # plan card (attached at the confirm turn). Do NOT say "the
        # attached photo" — no fresh attachment on this turn (Brent UX
        # 2026-07-01). Agents that resurface the photo at this turn get
        # dupes; agents that don't leave the operator wondering where
        # the photo went.
        # ONE decision, made looking at the fresh bed photo. No request-id (noise
        # for a single-user/single-printer setup), no double question — "bed's
        # clear, print it?" IS the bed affirmation. NO = keep the gcode staged
        # (it's already uploaded), not an abort.
        prompt = (
            "Sliced plate, review doc, and a fresh bed photo are attached. "
            "Bed clear and ready to print? Reply YES to start now, or NO to "
            "keep the gcode uploaded without printing."
        )
        need = {
            "stage": "need_input",
            "request_id": request_id,
            "need": "bed_clear_start",
            "key": "bed_clear_start",
            "requires_fresh_operator_bed_clear": True,
            "approval_prompt_key": "bed_clear_start",
            "prompt": prompt,
            "instruction": ("FIRST surface the kit_plate_preview + bed_snapshot "
                            "image paths BARE and attach the review_doc, THEN "
                            "the prompt — the operator decides looking at the "
                            "real bed. On NO: the gcode stays uploaded (staged), "
                            "nothing prints; acknowledge and stop."),
            "expected_answers": ["yes", "no"],
            # Short confirm token instead of a ~200-char verbatim command:
            # the model relays ONE opaque token, the workflow resolves the
            # request + nonce + full kit context from persisted state. This
            # stopped gemma4 mangling the request_id mid-command. Single-use;
            # the nonce still does the auth (revision + gcode binding).
            "next_command_on_yes": (
                f"python3 /opt/data/scripts/u1_kit_workflow.py "
                f"--confirm-start {confirm_token}"),
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
            if pending.get("nonce") and pending_nonce != pending.get("nonce"):
                problems.append(
                    "pending nonce missing/mismatched — the confirm call "
                    "must be the VERBATIM next_command_on_yes emitted at "
                    "the yes/no prompt (re-run --action start for a fresh "
                    "prompt)")
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
        # The workflow RUNS the gate itself — it does NOT hand the Stage-2
        # command back to the model to retype. A 26B model (gemma4) appended
        # garbage to the approval token when relaying this long token+nonce
        # command (live 2026-07-03), and the gate correctly refused. The human
        # already authorized via the bed-clear confirm; the workflow holds the
        # token + nonce. The 120s grace CANCEL is model-free (gateway hook
        # touches a marker the gate polls — docs/verify-cancel-hook.md), so
        # blocking here does NOT break cancel: the operator still gets the live
        # grace DM from the gate and can abort out-of-band.
        gate_py = "/opt/data/scripts/u1_print_start_gate.py"
        # Equals-form for the two random values: secrets.token_urlsafe can start
        # with '-', and argparse would treat a leading-dash VALUE as a flag
        # ("expected one argument"). This bug was latent in the old agent-relayed
        # command too (~1.5% of nonces). `--flag=value` is dash-safe.
        stage2_argv = [
            plate_filename,
            "--intended-tool", extruder,
            "--requested-material", material,
            "--request-id", request_id,
            "--bed-clear", "start",
            "--approval-token=" + token,
            "--stage2-approval-nonce=" + stage2_nonce,
        ]
        if operator:
            stage2_argv += ["--operator", operator]
        stage2_cmd = "python3 " + gate_py + " " + " ".join(
            _shell_quote(a) for a in stage2_argv)
        _emit(events_file, {
            "stage": "gate_invoked_by_workflow",
            "request_id": request_id,
            "reason": ("Operator confirmed bed-clear. Workflow runs the Stage 2 "
                       "gate directly (single-use nonce) — the safety-critical "
                       "command is never relayed by the model."),
            "command": stage2_cmd,
        }, json_events)
        _res = _invoke_stage2_gate(gate_py, stage2_argv,
                                   u1_request.ensure_request_dir(request_id))
        if getattr(_res, "stalled", False):
            # v2.2.1 #2: the gate is still running after the pre-grace wait but
            # never wrote a grace_started marker — it is stalled in a pre-grace
            # check (Moonraker query, camera, I/O) or heading to a late refusal.
            # Do NOT report a healthy grace window we cannot confirm. Surface the
            # honest unresolved state; the detached gate will finish on its own
            # and its outcome is in the log + audit trail.
            for _line in (_res.stdout or "").splitlines():
                _line = _line.strip()
                if _line.startswith("{"):
                    try:
                        _emit(events_file, json.loads(_line), json_events)
                    except Exception:
                        pass
            _emit(events_file, {
                "stage": "gate_state_unknown", "request_id": request_id,
                "reason": ("The start gate is taking longer than expected and has "
                           "not confirmed the grace window. It is running detached "
                           "and will finish on its own — do NOT re-run it. Check "
                           "the printer and the request audit log for the outcome."),
            }, json_events)
            return {"phase": "gate_state_unknown", "request_id": request_id,
                    "started": None}
        if _res is None:
            # Gate passed its pre-grace safety checks and is in the grace window,
            # running DETACHED. Blocking the agent's tool call through the full
            # ~120s grace timed it out at 60s and killed the gate mid-grace
            # (live 2026-07-04). The operator already has the grace DM from the
            # gate and reply-CANCEL works out-of-band; nothing more for the agent.
            _emit(events_file, {
                "stage": "grace_in_progress", "request_id": request_id,
                "reason": ("Print is in the pre-start grace window — reply CANCEL "
                           "to abort, or ignore to let it start. The gate runs to "
                           "completion on its own; do NOT re-run anything."),
            }, json_events)
            return {"phase": "grace_in_progress", "request_id": request_id,
                    "started": None}
        # Gate exited within the pre-grace wait (a fast refusal, or a fast
        # outcome) — surface its events + outcome.
        for _line in (_res.stdout or "").splitlines():
            if _line.strip():
                print(_line, flush=True)
        if (_res.stderr or "").strip():
            sys.stderr.write(_res.stderr)
        # Parse the outcome from the last JSON object the gate emitted.
        _outcome: dict[str, Any] = {}
        for _line in reversed((_res.stdout or "").splitlines()):
            _s = _line.strip()
            if _s.startswith("{"):
                try:
                    _o = json.loads(_s)
                except Exception:
                    continue
                if "started" in _o or "ok" in _o or _o.get("stage") in (
                        "start_attempt", "print_started", "start_cancelled",
                        "gate_refused_file_missing"):
                    _outcome = _o
                    break
        _started = bool(_outcome.get("started"))
        return {"phase": "print_started" if _started else "start_not_completed",
                "request_id": request_id,
                "gate_exit_code": _res.returncode,
                "started": _started,
                "gate_outcome": _outcome}
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
            "label": "Slice & review — then a fresh bed-clear yes/no starts the print",
            "value": "start",
            # Carry the FULL persisted answer set — a start command missing
            # orient/profile/supports would fall back into the staged Q&A
            # and re-slice (the Turn-1 fallback bug class).
            "next_command": _build_next_command(
                archive, request_id, parts=parts_answer, tool=tool_choice,
                material=material,
                orient=(state.get("kit") or {}).get("orient_mode"),
                profile=state.get("profile"),
                supports=state.get("supports"),
                action="start", nozzle=nozzle,
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
                                   pending_nonce: str | None = None,
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

    # Degraded-verification guard: the manual override exists for the case
    # where the camera path FAILED. When the confirm captured a real photo
    # and issued a token, the normal `start` path is strictly stronger —
    # refuse the override so an agent can't route around the photo, and
    # hand back the right command instead of a dead end.
    _safety_now = state.get("safety") or {}
    if _safety_now.get("approval_token") and _safety_now.get("bed_clear_photo_captured"):
        _audit(request_id, "operator_override_refused", operator,
               override_kind="manual_bed_check",
               reason="camera_verification_available",
               verification_method=verification_method,
               operator_text=operator_text)
        _emit(events_file, {
            "stage": "manual_bed_check_refused",
            "request_id": request_id,
            "reason": ("A real bed photo + approval token already exist for "
                       "this request — manual verification is only for the "
                       "degraded-camera case. Use the normal `start` action "
                       "(fresh yes/no against the captured photo)."),
        }, json_events)
        return {"phase": "manual_bed_check_refused", "request_id": request_id}

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
            "next_command_on_yes": (yes_command or (
                # Legacy fallback (state-recovery path) — should never be
                # taken; callers always pass yes_command with full context.
                f"python3 /opt/data/scripts/u1_kit_workflow.py "
                f"--request-id {request_id} --action 'start manual-bed-check' "
                f"--bed-clear-confirmed "
                f"--operator-text {_shell_quote(operator_text)} "
                f"--verification-method {_shell_quote(verification_method)}"
            )) + f" --pending-nonce {nonce}",
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
        if pending.get("nonce") and pending_nonce != pending.get("nonce"):
            problems.append(
                "pending nonce missing/mismatched — the confirm call must be "
                "the VERBATIM next_command_on_yes emitted at the yes/no "
                "prompt")
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
        ("supports", "Supports — turn on / off globally"),
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
                             answers_json: str | None,
                             answers_from: str | None = None) -> dict[str, Any]:
    """Single-call commit path. Three intakes, one validation core:
    --form-answers (operator's one line), --form-answers-json (structured),
    --form-answers-from <form_id> (v2.2 file handoff — the gateway wrote
    the answers; the model never carried them)."""
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

    if answers_from:
        # File redemption is nonce-like: the id must match the form this
        # request emitted (when one was emitted), and the file is consumed
        # on read — a replayed command redeems nothing.
        persisted_form_id = existing.get("form_id")
        if persisted_form_id and answers_from != persisted_form_id:
            msg = ("form id mismatch — this request is awaiting a different "
                   "form. Re-run the workflow to get a fresh form.")
            _emit(events_file, {"stage": "form_rejected", "key": "kit_form",
                                "request_id": request_id, "errors": [msg]},
                  json_events)
            _audit(request_id, "form_answers_rejected", operator,
                   reason="form_id_mismatch", given=str(answers_from)[:32])
            return {"phase": "form_rejected", "request_id": request_id,
                    "errors": [msg]}
        try:
            obj = u1_form.read_and_consume_answers(answers_from)
        except (FileNotFoundError, ValueError, OSError) as exc:
            msg = f"could not redeem answers file: {exc}"
            _emit(events_file, {"stage": "form_rejected", "key": "kit_form",
                                "request_id": request_id, "errors": [msg]},
                  json_events)
            _audit(request_id, "form_answers_rejected", operator,
                   reason="file_redeem_failed", error=str(exc)[:200])
            return {"phase": "form_rejected", "request_id": request_id,
                    "errors": [msg]}
        _audit(request_id, "form_answers_file_redeemed", operator,
               form_id=answers_from)
        parsed = u1_form.parse_answers_json(obj, spec)
    elif answers_json:
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
    if answers_from:
        # The pending form is satisfied — clear the binding so a future
        # form-mode run mints a fresh id instead of reusing a spent one.
        u1_request.write_request(request_id, form_id=None)
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

    # v2.3 advanced overrides (from the form's Advanced screen). Applied on top
    # of the supports patch — each pass flattens, so the temp stays
    # self-contained. The review doc's full-config sweep marks every override
    # as DIFFERS automatically, so the operator sees them before the yes.
    adv_overrides = values.get("overrides") or {}
    if adv_overrides:
        process = apply_profile_overrides(process, adv_overrides, out_dir)
        _audit(request_id, "advanced_overrides_applied", operator,
               **{k: str(v) for k, v in adv_overrides.items()})

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

    kit_stem = u1_kit._sanitize(_strip_doc_prefix(archive.stem))
    live = bool(getattr(args, "live_upload", False))
    plates_state: list[dict[str, Any]] = []
    upload_failures: list[dict[str, Any]] = []
    # Same own-prior-name overwrite rule as the staged path: re-uploading
    # this request's own deterministic plate names must not rc=5 dead-end.
    _own_prior_names: set[str] = set()
    try:
        for _p in ((u1_request.read_request(request_id) or {}).get("plates") or []):
            if _p.get("printer_storage_filename"):
                _own_prior_names.add(_p["printer_storage_filename"])
    except Exception:
        pass
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
        _oc = getattr(args, "on_collision", None)
        if _oc is None:
            # Re-uploading THIS request's own prior name (adjust -> re-confirm)
            # overwrites it. ANY OTHER collision -> rename with a timestamp
            # suffix so the upload never fails and never clobbers a different
            # job's file. This is the common case now that doc-hash filename
            # prefixes are dropped (82a9681): re-printing a model a PRIOR
            # request already uploaded = same base name. rename appends the
            # timestamp ONLY on collision, so the first print keeps the clean
            # name and the model name still leads (printer-list readability).
            _oc = "overwrite" if named.name in _own_prior_names else "rename"
        up = _real_upload(named,
                          on_collision=_oc,
                          material=material) if live else {
            "dry_run": True, "uploaded_filename": named.name, "moonraker_upload_ok": None}
        # Same rc contract as the staged path (H1): rc 2/4/5 = file did NOT
        # land; rc=3 = landed with state warnings for Stage 2. Ignoring the
        # rc here meant a dead Moonraker still produced kit_uploaded +
        # "all plates on the printer".
        if live:
            _rc = int(up.get("returncode", -1) or -1)
            if _rc in (2, 4, 5) or up.get("moonraker_upload_ok") is False:
                upload_failures.append({
                    "plate_idx": idx,
                    "filename": named.name,
                    "returncode": _rc,
                    "moonraker_upload_ok": up.get("moonraker_upload_ok"),
                    "human_summary": up.get("human_summary"),
                })
        post_inject_hash = (u1_request.compute_model_hash(named)
                            if injection.get("ok") else pl["gcode_hash"])
        _arranged = pl.get("arranged_stls") or []
        plates_state.append({
            "plate_idx": idx,
            "gcode_path": str(named),
            "gcode_hash": post_inject_hash,
            "printer_storage_filename": up.get("uploaded_filename") or named.name,
            "uploaded": up,
            "started": False,
            # metadata (est time + filament) + partition_parts were missing on
            # this (form-mode) path, so the review doc showed "—" and no parts
            # row — unlike the staged path (operator feedback 2026-07-03).
            "metadata": pl.get("metadata", {}),
            "partition_parts": [re.sub(r"^obj_\d+_", "", os.path.basename(s))
                                for s in _arranged],
            "arranged_stls": _arranged,
            "preview_path": layout.get("path"),
            "iso_path": layout.get("iso_path"),
            "thumbnail_injection": injection,
        })
    if upload_failures:
        _emit(events_file, {"stage": "kit_upload_failed",
                            "request_id": request_id,
                            "failures": upload_failures,
                            "instruction": ("One or more plates did not land "
                                            "on the printer. Fix the underlying "
                                            "issue (printer reachable? storage "
                                            "full? filename collision?), then "
                                            "re-send the form answers.")},
              json_events)
        _audit(request_id, "kit_upload_failed", operator,
               failure_count=len(upload_failures))
        u1_request.write_request(request_id, phase="upload_failed",
                                 plates=plates_state)
        return {"phase": "upload_failed", "request_id": request_id,
                "failures": upload_failures}

    _emit(events_file, {"stage": "kit_uploaded", "request_id": request_id,
                        "plates": [p["printer_storage_filename"] for p in plates_state],
                        "live": live}, json_events)

    # Surface the sliced-plate layout render(s) so the operator sees the
    # arranged parts on the bed — the staged path does this; the form path was
    # computing the preview but never emitting it (operator 2026-07-03: "I never
    # got the photo of the sliced render").
    for _ps in plates_state:
        _pv = _ps.get("preview_path")
        if _pv and os.path.isfile(_pv):
            _emit(events_file, {"stage": "render", "request_id": request_id,
                                "kind": "kit_plate_preview",
                                "plate_idx": _ps.get("plate_idx"),
                                "image": _pv,
                                "instruction": ("Surface this image path BARE (no "
                                                "backticks) in your reply so the "
                                                "operator sees the sliced plate "
                                                "layout before the bed-clear prompt.")},
                  json_events)
        _iso = _ps.get("iso_path")
        if _iso and os.path.isfile(_iso):
            _emit(events_file, {"stage": "render", "request_id": request_id,
                                "kind": "kit_plate_isometric",
                                "plate_idx": _ps.get("plate_idx"),
                                "image": _iso,
                                "instruction": ("Surface this 3D view path BARE too "
                                                "(alongside the top-down plate) — it "
                                                "shows the operator the real print "
                                                "pose to sanity-check before start.")},
                  json_events)

    plate1 = plates_state[0]
    _tidx = _tool_to_index(tool)
    extruder = "extruder" if _tidx == 0 else f"extruder{_tidx}"
    stage1_cmd = build_stage1_command(
        printer_filename=plate1["printer_storage_filename"],
        intended_tool=extruder, material=material, request_id=request_id,
    )

    action = values.get("action", "start")

    # Human-readable profile name + part filenames for the review doc, so it
    # reads like the sample and not raw slugs/IDs (operator feedback
    # 2026-07-03: "0_20_strength_gyroid" and "04_angle_90" looked generated).
    _pf = (u1_request.read_request(request_id) or {}).get("form_profiles") or []
    _profile_label = next((p.get("label") for p in _pf
                           if p.get("value") == profile_slug), None) or profile_slug
    _parts_display = ", ".join(p.get("filename", p["part_id"]) for p in selected)

    # Pre-print review doc (v2.2) — same artifact as the staged path,
    # same fail-soft contract.
    review_doc_path: str | None = None
    try:
        review_doc_path = str(u1_review_doc.generate(
            request_id, out_dir, plates_state,
            state=u1_request.read_request(request_id) or {},
            decisions={"tool": tool, "material": material,
                       "profile": _profile_label,
                       "orient": values.get("orient"),
                       "supports": supports,
                       "parts": _parts_display},
            operator=operator,
            reference=u1_review_doc.build_reference(
                profile_slug, material, nozzle=nozzle, out_dir=out_dir),
            envelope=u1_review_doc.build_material_envelope(
                material, nozzle=nozzle, out_dir=out_dir),
        ))
        _emit(events_file, {
            "stage": "review_doc", "request_id": request_id,
            "path": review_doc_path,
            "instruction": ("Attach this file to the operator alongside the "
                            "readiness card — human-readable review of "
                            "exactly what will print."),
        }, json_events)
    except Exception as _rd_exc:
        _audit(request_id, "review_doc_failed", operator,
               error=f"{type(_rd_exc).__name__}: {_rd_exc}"[:200])

    readiness = {
        "stage": "kit_readiness_card",
        "review_doc_path": review_doc_path,
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
        # start_gate_stage1_command is deliberately NOT surfaced here — a
        # confused agent grabbed it and drove the gate directly (Stage 1 then a
        # Stage 2 with no nonce → correctly refused, the scary "Stage 2 refused"
        # the operator kept hitting on the FORM path). The ONLY start path is
        # the `start` option's --action start command; the workflow drives the
        # bed-clear yes/no and emits the nonce-bound Stage 2 itself. Still
        # persisted in request state (write_request below) for the fallback.
        "operator_guidance": (
            f"{len(plates_state)} plate(s). Stage 1 gates ONLY plate 1 "
            f"({plate1['printer_storage_filename']}). After it prints, start plates "
            f"2..{len(plates_state)} from the Snapmaker app — they're already uploaded."
            if len(plates_state) > 1 else
            "Single plate. Stage 1 captures the bed photo + approval token."
        ),
    }
    _emit(events_file, readiness, json_events)

    # Persist plate + plan state BEFORE any bed-clear routing so _action_start
    # (below) reads plate_filename / gcode_hash / tool / material from state.
    persist_phase = "awaiting_confirm" if action == "start" else "complete"
    u1_request.write_request(
        request_id,
        phase=persist_phase,
        kit={"parts": kit["parts"], "part_count": kit["part_count"],
             "selected": [p["part_id"] for p in selected], "orient_mode": values.get("orient")},
        plates=plates_state,
        tool=tool, material=material, profile=profile_slug, supports=override,
        overrides=values.get("overrides") or {},
        gcode_hash=plate1["gcode_hash"],
        printer_storage_filename=plate1["printer_storage_filename"],
        start_gate_stage1_command=stage1_cmd,
        readiness_card_event=readiness,
    )
    _audit(request_id, "kit_readiness_card_emitted", operator,
           plate_count=len(plates_state), gated_plate=plate1["printer_storage_filename"],
           gcode_hash=plate1["gcode_hash"],
           request_revision=(u1_request.read_request(request_id) or {}).get("request_revision", 1))

    if action != "start":
        _emit(events_file, {"stage": "complete", "request_id": request_id,
                            "reason": ("Upload-only: all plates on the printer; "
                                       "start from the Snapmaker app."
                                       if live else
                                       "Upload-only DRY RUN: plates sliced "
                                       "locally, nothing sent to the printer "
                                       "(pass --live-upload to send).")}, json_events)
        return {"phase": "complete", "request_id": request_id,
                "out_dir": str(out_dir), "plate_count": len(plates_state),
                "gated_plate": plate1["printer_storage_filename"]}

    # action == "start": capture the bed photo + issue a token, then route into
    # the SAME two-turn bed-clear gate as the staged path. _action_start emits
    # the yes/no and mints the Stage-2 nonce on yes. The form path used to hand
    # the agent the raw Stage-1 gate command with NO nonce path, so the kit gate
    # refused EVERY start ("Stage 2 refused", operator 2026-07-03, every run).
    _no_live_upload = not live
    _no_live_material = bool(getattr(args, "no_live_material", False))
    bed_result = _capture_bed_and_issue_token(out_dir)
    if not bed_result["ok"]:
        reason = str(bed_result.get("reason") or "camera unreachable")
        _emit(events_file, {
            "stage": "need_input", "key": "confirm", "request_id": request_id,
            "prompt": (f"Bed photo could not be captured ({reason}). Can't offer a "
                       "gated start. Reply `upload-only` to keep the sliced plates, "
                       "or fix the camera and re-run the kit."),
            "options": [{"label": "Upload only — keep plates, don't print",
                         "value": "upload-only",
                         "next_command": _build_next_command(
                             archive, request_id, action="upload-only", nozzle=nozzle,
                             no_live_upload=_no_live_upload,
                             no_live_material=_no_live_material)}],
        }, json_events)
        return {"phase": "awaiting_confirm", "request_id": request_id,
                "out_dir": str(out_dir), "bed_capture_failed": True}

    _safety = dict((u1_request.read_request(request_id) or {}).get("safety") or {})
    _safety.update({
        "bed_clear_check_required": True,
        "bed_clear_photo_captured": True,
        "bed_clear_photo_path": bed_result["snapshot_path"],
        "snapshot_path": bed_result["snapshot_path"],
        "approval_token": bed_result["token"],
    })
    u1_request.write_request(request_id, safety=_safety)
    _emit(events_file, {"stage": "render", "request_id": request_id,
                        "kind": "bed_snapshot", "image": bed_result["snapshot_path"],
                        "instruction": ("Surface this bed photo path BARE (no backticks) "
                                        "in your reply BEFORE the bed-clear yes/no "
                                        "prompt - the prompt refers to 'the bed photo I "
                                        "sent', so it must actually be sent.")},
          json_events)
    yes_command = _build_next_command(
        archive, request_id, action="start", nozzle=nozzle,
        no_live_upload=_no_live_upload, no_live_material=_no_live_material
    ) + " --bed-clear-confirmed"
    return _action_start(events_file, request_id, json_events,
                         yes_command=yes_command, bed_clear_confirmed=False,
                         operator=operator)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Multi-part kit slice workflow (Snapmaker U1)")
    ap.add_argument("model", nargs="?", default=None,
                    help=("zip of STLs (a kit) or a single model file. "
                          "Optional ONLY when --request-id resolves a recoverable "
                          "model_path from request.json (resume case)."))
    ap.add_argument("--json-events", action="store_true")
    ap.add_argument("--reprint", action="store_true",
                    help="List recent prints to print again (no model file needed)")
    ap.add_argument("--reprint-start", default=None, metavar="TOKEN",
                    dest="reprint_start",
                    help="Start the reprint the operator picked "
                         "(single-use token minted by --reprint)")

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
                    choices=["supports", "no_supports", "no-supports"],
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
    ap.add_argument("--form-answers-from", default=None, dest="form_answers_from",
                    help=("v2.2 file handoff: redeem a gateway-written answers "
                          "file by form_id (single-use, bound to the form this "
                          "request emitted). The model relays this command "
                          "verbatim; answer content never passes through it."))
    ap.add_argument("--redeem-pending-form", action="store_true",
                    dest="redeem_pending_form", default=False,
                    help=("Redeem the pending form WITHOUT relaying its form_id — "
                          "the workflow reads form_id off the request. Preferred "
                          "over --form-answers-from for model-relayed redeems: a "
                          "26B model mangled the random-hex id verbatim "
                          "(live 2026-07-03)."))
    ap.add_argument("--confirm-start", default=None, dest="confirm_start",
                    help=("v2.2 bed-clear confirm: the operator answered 'yes' "
                          "at the bed-clear prompt. The model relays ONLY this "
                          "short token (it mangled the old long command); the "
                          "workflow resolves the request + single-use nonce "
                          "from persisted state. Never hand-assemble it."))
    ap.add_argument("--pending-nonce", default=None, dest="pending_nonce",
                    help=("Single-use nonce from the emitted "
                          "next_command_on_yes. The confirm call must present "
                          "it — copy the emitted command VERBATIM; a "
                          "hand-assembled confirm is refused."))
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
                          "with form_schema (buttons UX, requires the u1-form "
                          "Hermes plugin). Falls through to env U1_INTERACTION_MODE "
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
