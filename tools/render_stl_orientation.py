#!/usr/bin/env python3
"""Render a 4-view orientation review of an STL — isometric, front, side,
top — with overhang-flagged faces highlighted and a header showing bounding
box, Z range, and the count of overhang triangles.

Designed to give an agent or operator a quick "is this orientation
printable, and where will it need supports?" answer before slicing. The
overhang faces highlighted here are the same ones a slicer like Orca will
warn about during pre-slice validation.

Requires: PIL (Pillow) + numpy. Pure stdlib otherwise.

Example:
    python3 tools/render_stl_orientation.py model.stl --out review.png

    # bigger render + tighter overhang threshold + custom title
    python3 tools/render_stl_orientation.py model.stl \\
        --out review.png --width 1800 --height 1400 \\
        --overhang-threshold -0.15 \\
        --title "Orbital sander vacuum attachment"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Sibling tool import.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from _stl_render import (  # noqa: E402
    DEFAULT_OVERHANG,
    bbox,
    overhang_mask,
    parse_stl,
    render_view,
)

# Dark UI palette (matches the look in the README's example image).
BG = (22, 26, 31)
PANEL_BG = (31, 36, 43)
TEXT = (232, 238, 245)
MUTED = (160, 170, 180)
ACCENT_ORANGE = DEFAULT_OVERHANG

VIEWS = ("iso", "front", "side", "top")
VIEW_LABELS = {
    "iso": "ISOMETRIC",
    "front": "FRONT",
    "side": "SIDE",
    "top": "SOURCE STL VIEW (as-authored, NOT what the slicer will use)",
}


def _load_font(size: int) -> ImageFont.ImageFont:
    """Try a small set of common TTFs; fall back to PIL's bitmap default
    if none are installed. The bitmap default is small but readable."""
    for candidate in (
        "DejaVuSans.ttf", "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "Arial.ttf", "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _format_header(tris: np.ndarray, mask: np.ndarray, title: str | None) -> list[tuple[str, tuple[int, int, int]]]:
    """Build the header text lines as (text, color) tuples."""
    xmin, xmax, ymin, ymax, zmin, zmax = bbox(tris)
    # ASCII 'x' so we render correctly on PIL's bitmap default font when
    # no TTF is installed — saves a font-coverage gotcha for community
    # users on minimal Python images.
    dims = f"{xmax - xmin:.1f} x {ymax - ymin:.1f} x {zmax - zmin:.1f} mm"
    n_total = int(tris.shape[0])
    n_overhang = int(mask.sum()) if mask is not None else 0
    overhang_pct = (n_overhang / n_total * 100) if n_total else 0.0
    z_min_at_bed = "on bed" if abs(zmin) < 0.01 else f"{zmin:+.2f} mm"
    lines: list[tuple[str, tuple[int, int, int]]] = []
    if title:
        lines.append((title.upper(), TEXT))
    lines.append((f"DIMS {dims}    Z MIN {z_min_at_bed}    {n_total} TRIS    OVERHANG {n_overhang} ({overhang_pct:.1f}%)", MUTED))
    lines.append(("ORANGE = DOWNWARD-FACING FACES (likely need supports)", ACCENT_ORANGE))
    return lines


def render_orientation_sheet(
    tris: np.ndarray,
    width: int = 1800,
    height: int = 1400,
    *,
    title: str | None = None,
    overhang_threshold: float = -0.3,
) -> Image.Image:
    """Compose a 2x2 grid of (iso, front, side, top) views with a header
    band on top describing bounding box + overhang stats."""
    mask = overhang_mask(tris, threshold=overhang_threshold)

    # Layout: header band + 2x2 grid + bottom margin
    header_h = 130 if title else 100
    margin = 24
    panel_w = (width - margin * 3) // 2
    panel_h = (height - header_h - margin * 3) // 2
    sheet = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(sheet)

    # Header text
    title_font = _load_font(28)
    body_font = _load_font(18)
    y = margin
    for text, color in _format_header(tris, mask, title):
        draw.text((margin, y), text, fill=color, font=title_font if color == TEXT else body_font)
        y += 36 if color == TEXT else 26

    # 2x2 grid
    positions = [
        (margin, header_h + margin),                           # iso (top-left)
        (margin + panel_w + margin, header_h + margin),         # front (top-right)
        (margin, header_h + margin + panel_h + margin),         # side (bottom-left)
        (margin + panel_w + margin,
         header_h + margin + panel_h + margin),                 # top (bottom-right)
    ]

    for view, (px, py) in zip(VIEWS, positions):
        # Panel background
        draw.rectangle([px, py, px + panel_w, py + panel_h], fill=PANEL_BG)
        # The rendered view fills MOST of the panel (leave 32px for label at top)
        label_h = 32
        view_img = render_view(
            tris, panel_w, panel_h - label_h,
            view=view,
            overhang_flags=mask,
            bg=PANEL_BG,
        )
        sheet.paste(view_img, (px, py + label_h))
        # Panel label
        draw.text((px + 12, py + 6), VIEW_LABELS[view], fill=MUTED, font=body_font)

    return sheet


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("stl", type=Path, help="Source STL (binary or ASCII).")
    ap.add_argument("--for-slice-review", action="store_true",
                    help="Shim to the v1.4.0 slice-review renderer; render the exact oriented STL used for slicing.")
    ap.add_argument("--gcode", type=Path, default=None,
                    help="Optional G-code for --for-slice-review first-layer footprint extraction.")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output PNG path. Default: <stl-stem>_orientation.png next to the STL.")
    ap.add_argument("--width", type=int, default=1800, help="Output width in px. Default: 1800.")
    ap.add_argument("--height", type=int, default=1400, help="Output height in px. Default: 1400.")
    ap.add_argument("--title", default=None, help="Optional title shown in the header band.")
    ap.add_argument("--overhang-threshold", type=float, default=-0.3,
                    help="Face-normal Z below this = overhang. -0.3 ≈ 17° below horizontal. "
                         "Closer to 0 = stricter. Default: -0.3.")
    args = ap.parse_args(argv)

    if not args.stl.exists() or not args.stl.is_file():
        print(f"STL not found: {args.stl}", file=sys.stderr)
        return 2

    if args.for_slice_review:
        from render_slice_review import render_slice_review
        out = args.out or args.stl.with_name(args.stl.stem + "_slice_review.png")
        render_slice_review(args.stl, out, gcode=args.gcode, title=args.title or args.stl.stem.replace("_", " "))
        print(f"Slice review -> {out}")
        return 0

    tris = parse_stl(args.stl)
    if tris.shape[0] == 0:
        print(f"STL has no triangles: {args.stl}", file=sys.stderr)
        return 3

    title = args.title or args.stl.stem.replace("_", " ")
    sheet = render_orientation_sheet(
        tris, args.width, args.height,
        title=title,
        overhang_threshold=args.overhang_threshold,
    )

    out = args.out or args.stl.with_name(args.stl.stem + "_orientation.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, format="PNG", optimize=True)

    xmin, xmax, ymin, ymax, zmin, zmax = bbox(tris)
    n_total = int(tris.shape[0])
    n_overhang = int(overhang_mask(tris, args.overhang_threshold).sum())
    print(
        f"Orientation review -> {out}\n"
        f"  dims: {xmax - xmin:.1f} x {ymax - ymin:.1f} x {zmax - zmin:.1f} mm "
        f"(Z {zmin:.2f} to {zmax:.2f})\n"
        f"  {n_total} triangles, {n_overhang} flagged as overhang "
        f"({n_overhang / n_total * 100:.1f}% — likely need supports)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
