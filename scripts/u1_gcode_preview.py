#!/usr/bin/env python3
"""Isometric toolpath preview rendered straight from sliced gcode.

Experiment (feature/gcode-truth-previews): the shipped 3D review image keeps
only each part's outer-wall silhouette, so supports are invisible and a part
lying on its side can read like one standing up. This renders the actual
toolpaths instead: every extrusion move, grouped by Orca's ``;TYPE:`` labels
and ``M486`` part markers, drawn bottom-up in a true isometric projection.
Supports get their own color, so "where are the supports" is answered by the
image the operator already receives, and the silhouette of the real layers
shows the true print pose. The gcode is the ground truth the printer runs,
so nothing is re-sliced or re-arranged to make the picture.

Standalone CLI for experimentation:

    python3 u1_gcode_preview.py plate_1.gcode out.png [--title "Plate 1"]
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Parsing: extrusion segments tagged with part + feature type
# --------------------------------------------------------------------------- #

_NUM = r"(-?\d*\.?\d+)"
_G_RE = re.compile(r"^G[0123]\b")
_G23_RE = re.compile(r"^G[23]\b")
_X_RE = re.compile(r"\bX" + _NUM)
_Y_RE = re.compile(r"\bY" + _NUM)
_Z_RE = re.compile(r"\bZ" + _NUM)
_E_RE = re.compile(r"\bE" + _NUM)
_I_RE = re.compile(r"\bI" + _NUM)
_J_RE = re.compile(r"\bJ" + _NUM)
_M486_A = re.compile(r"^M486 A(.+?)\s*$")
_M486_S = re.compile(r"^M486 S(-?\d+)")

# Feature categories, in back-to-front draw order within a layer. Everything
# supports-ish shares one bucket so the operator reads one color as "support".
_TYPE_TO_CAT = {
    "Brim": "brim",
    "Skirt": "brim",
    "Support": "support",
    "Support interface": "support",
    "Support transition": "support",
    "Bottom surface": "bottom",
    "Internal solid infill": None,       # interior noise at preview scale
    "Sparse infill": None,
    "Internal Bridge": None,
    "Inner wall": "inner",
    "Outer wall": "outer",
    "Overhang wall": "outer",
    "Bridge": "outer",
    "Gap infill": None,
    "Top surface": "top",
    "Custom": None,                      # start/end/purge blocks
}
_CAT_ORDER = {"brim": 0, "support": 1, "bottom": 2, "inner": 3, "outer": 4, "top": 5}

_META_RE = {
    "filament_g": re.compile(r"^; total filament used \[g\] = " + _NUM),
    "time": re.compile(r"^; estimated printing time \(normal mode\) = (.+)$"),
}


def parse_toolpaths(gcode_path: Path) -> dict[str, Any]:
    """Extract every extrusion segment from ``gcode_path``.

    Returns ``{"segments": [(z, seq, cat, part, x0, y0, x1, y1), ...],
    "parts": [base names in first-seen order], "meta": {...}}``. Arc moves
    (G2/G3) are flattened to chords the same way the shipped M486 renderer
    does, so both previews corroborate. Extrusion is any G move with E > 0,
    which holds for the relative-E gcode the U1 profiles emit.
    """
    segments: list[tuple] = []
    id_to_name: dict[int, str] = {}
    part_order: list[str] = []
    meta: dict[str, Any] = {}
    cid: int | None = None
    cbase: str | None = None
    ctype: str | None = None
    prevx = prevy = None
    cz = 0.0
    seq = 0

    for ln in Path(gcode_path).read_text(errors="replace").splitlines():
        if ln.startswith(";"):
            if ln.startswith(";TYPE:"):
                ctype = ln[6:].strip()
                continue
            for key, rx in _META_RE.items():
                if key not in meta:
                    m = rx.match(ln)
                    if m:
                        meta[key] = m.group(1)
            continue
        ma = _M486_A.match(ln)
        if ma:
            if cid is not None:
                id_to_name[cid] = ma.group(1).strip()
            continue
        ms = _M486_S.match(ln)
        if ms:
            nid = int(ms.group(1))
            cid = None if nid < 0 else nid
            nm = id_to_name.get(cid, "") if cid is not None else ""
            cbase = re.sub(r"_id_\d+_copy_\d+$", "", nm) or (nm or None)
            if cbase and cbase not in part_order:
                part_order.append(cbase)
            continue
        if not _G_RE.match(ln):
            continue
        zm = _Z_RE.search(ln)
        if zm:
            cz = float(zm.group(1))
        xm = _X_RE.search(ln)
        ym = _Y_RE.search(ln)
        em = _E_RE.search(ln)
        nx = float(xm.group(1)) if xm else prevx
        ny = float(ym.group(1)) if ym else prevy
        cat = _TYPE_TO_CAT.get(ctype or "", None)
        if (em and float(em.group(1)) > 0 and cat and prevx is not None
                and nx is not None and ny is not None):
            if _G23_RE.match(ln):
                im = _I_RE.search(ln)
                jm = _J_RE.search(ln)
                cx = prevx + (float(im.group(1)) if im else 0.0)
                cy = prevy + (float(jm.group(1)) if jm else 0.0)
                r = math.hypot(prevx - cx, prevy - cy)
                sa = math.atan2(prevy - cy, prevx - cx)
                ea = math.atan2(ny - cy, nx - cx)
                if ln.startswith("G2"):
                    if ea > sa:
                        ea -= 2 * math.pi
                    sweep = sa - ea
                else:
                    if ea < sa:
                        ea += 2 * math.pi
                    sweep = ea - sa
                nseg = max(2, int(abs(sweep) * r / 1.0))
                px, py = prevx, prevy
                for k in range(1, nseg + 1):
                    a = sa - sweep * k / nseg if ln.startswith("G2") else sa + sweep * k / nseg
                    qx, qy = cx + r * math.cos(a), cy + r * math.sin(a)
                    segments.append((cz, seq, cat, cbase, px, py, qx, qy))
                    seq += 1
                    px, py = qx, qy
            else:
                segments.append((cz, seq, cat, cbase, prevx, prevy, nx, ny))
                seq += 1
        prevx, prevy = nx, ny

    return {"segments": segments, "parts": part_order, "meta": meta}


# --------------------------------------------------------------------------- #
# Rendering: true isometric, painter's algorithm bottom-up
# --------------------------------------------------------------------------- #

_COS30 = math.cos(math.radians(30))
_SIN30 = math.sin(math.radians(30))


def _iso(x: float, y: float, z: float) -> tuple[float, float]:
    """Project bed-space mm to isometric plane units (y grows downward later)."""
    return (x - y) * _COS30, (x + y) * _SIN30 - z


def _part_colors(parts: list[str]) -> dict[str, tuple[int, int, int]]:
    import colorsys
    colors: dict[str, tuple[int, int, int]] = {}
    for i, p in enumerate(parts):
        h = (i * 0.618034) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, 0.55, 0.88)
        colors[p] = (int(r * 255), int(g * 255), int(b * 255))
    return colors


def _dim(c: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    return tuple(int(v * f) for v in c)  # type: ignore[return-value]


def _lift(c: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    return tuple(int(v + (255 - v) * f) for v in c)  # type: ignore[return-value]


_SUPPORT_COLOR = (255, 158, 60)
_BRIM_COLOR = (88, 99, 112)
_BED_GRID = (43, 50, 60)
_BED_EDGE = (66, 76, 90)
_BG = (22, 26, 31)


def render_iso_preview(
    gcode_path: Path,
    out_path: Path,
    *,
    bed_mm: tuple[float, float] = (270.0, 270.0),
    canvas_px: int = 1200,
    title: str | None = None,
) -> dict[str, Any]:
    """Render ``gcode_path`` to ``out_path`` as an isometric toolpath preview.

    Best-effort like the shipped renderers: every failure returns
    ``{"ok": False, "error": ...}`` so a caller can fall back to the old view.
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # pragma: no cover - deps guard
        return {"ok": False, "path": None, "error": f"deps: {exc}"}
    try:
        parsed = parse_toolpaths(gcode_path)
    except Exception as exc:
        return {"ok": False, "path": None, "error": f"gcode parse: {exc}"}
    segs = parsed["segments"]
    if not segs:
        return {"ok": False, "path": None, "error": "no extrusion segments found"}

    colors = _part_colors(parsed["parts"])
    support_segs = sum(1 for s in segs if s[2] == "support")

    # Frame on the printed material, not the whole bed: orientation and
    # supports are this view's job, placement is the top-down's.
    pts = []
    for z, _seq, _cat, _part, x0, y0, x1, y1 in segs:
        pts.append(_iso(x0, y0, z))
        pts.append(_iso(x1, y1, z))
    us = [p[0] for p in pts]
    vs = [p[1] for p in pts]
    umin, umax = min(us), max(us)
    vmin, vmax = min(vs), max(vs)
    span = max(umax - umin, vmax - vmin, 1.0)
    pad = span * 0.10
    umin -= pad
    umax += pad
    vmin -= pad
    vmax += pad
    header = 56 if title else 0
    footer = 44
    draw_h = canvas_px - header - footer
    scale = min(canvas_px / (umax - umin), draw_h / (vmax - vmin))

    def to_px(x: float, y: float, z: float) -> tuple[float, float]:
        u, v = _iso(x, y, z)
        return ((u - umin) * scale, header + (vmax - v) * scale)

    img = Image.new("RGB", (canvas_px, canvas_px), _BG)
    draw = ImageDraw.Draw(img)

    # Bed grid at z=0 under the model, clipped by the frame automatically.
    bx, by = bed_mm
    step = 50.0
    x = 0.0
    while x <= bx + 1e-6:
        draw.line([to_px(x, 0, 0), to_px(x, by, 0)],
                  fill=_BED_EDGE if x in (0.0, bx) else _BED_GRID, width=1)
        x += step
    y = 0.0
    while y <= by + 1e-6:
        draw.line([to_px(0, y, 0), to_px(bx, y, 0)],
                  fill=_BED_EDGE if y in (0.0, by) else _BED_GRID, width=1)
        y += step

    # Painter's algorithm: strict layer order carries the depth story, and
    # within a layer back-to-front category order keeps part edges crisp.
    segs.sort(key=lambda s: (s[0], _CAT_ORDER[s[2]], s[1]))
    for z, _seq, cat, part, x0, y0, x1, y1 in segs:
        base = colors.get(part, (150, 160, 170))
        if cat == "support":
            fill, width = _SUPPORT_COLOR, 2
        elif cat == "brim":
            fill, width = _BRIM_COLOR, 1
        elif cat == "inner":
            fill, width = _dim(base, 0.45), 1
        elif cat == "bottom":
            fill, width = _dim(base, 0.70), 1
        elif cat == "top":
            fill, width = _lift(base, 0.35), 2
        else:  # outer
            fill, width = base, 2
        draw.line([to_px(x0, y0, z), to_px(x1, y1, z)], fill=fill, width=width)

    # Header / footer text with the DejaVu fallback the workflow uses.
    try:
        from PIL import ImageFont
        try:
            f_big = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
            f_small = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 17)
        except Exception:
            f_big = f_small = ImageFont.load_default()
        if title:
            draw.text((16, 14), title, fill=(235, 238, 242), font=f_big)
        meta = parsed["meta"]
        bits = []
        if meta.get("filament_g"):
            bits.append(f"{meta['filament_g']} g filament")
        if meta.get("time"):
            bits.append(meta["time"])
        bits.append("supports shown in orange" if support_segs
                    else "no supports in this plate")
        draw.text((16, canvas_px - 32), "  ·  ".join(bits),
                  fill=(170, 178, 188), font=f_small)
    except Exception:
        pass  # text is garnish; the geometry is the payload

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return {"ok": True, "path": str(out_path), "segments": len(segs),
            "support_segments": support_segs, "parts": parsed["parts"],
            "meta": parsed["meta"]}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("gcode", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--title", default=None)
    ap.add_argument("--bed", default="270x270",
                    help="bed size in mm, WIDTHxDEPTH (default 270x270)")
    a = ap.parse_args(argv)
    bw, _, bd = a.bed.partition("x")
    res = render_iso_preview(a.gcode, a.out, title=a.title,
                             bed_mm=(float(bw), float(bd or bw)))
    print(res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
