#!/usr/bin/env python3
"""Inject PrusaSlicer/Orca-compatible thumbnail blocks into a sliced G-code.

OrcaSlicer's CLI doesn't render thumbnails (GUI-only code path), so even
though the sliced G-code declares `; thumbnails = 48x48/PNG, 300x300/PNG`,
the actual `; thumbnail begin ... end` blocks are missing. Snapmaker's app +
Moonraker parse those blocks to show a preview; without them, you get a
generic file icon.

This script does what the GUI would have done:
  1. Parse the STL (binary or ASCII)
  2. Project to 2D using an isometric-ish orthographic projection
  3. Render with PIL — flat Lambertian shading per face, painter's algorithm
  4. Encode PNG → base64
  5. Splice `; thumbnail begin W x H N\\n; <b64>\\n; thumbnail end` blocks
     into the G-code header

Requires: PIL (Pillow) + numpy. Pure stdlib otherwise.

Example:
    python3 tools/gcode_inject_thumbnail.py \\
        --stl model.stl --gcode plate_1.gcode \\
        --sizes 48x48,300x300 --in-place
"""
from __future__ import annotations

import argparse
import base64
import io
import re
import struct
import sys
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageDraw


# ---------- STL parsing ----------

def _is_ascii_stl(head: bytes) -> bool:
    return head[:5].lower() == b"solid" and b"facet" in head[:1024].lower()


def parse_stl(path: Path) -> np.ndarray:
    """Return triangles as ndarray of shape (N, 3, 3) — N triangles, 3 verts, xyz."""
    with path.open("rb") as f:
        head = f.read(1024)
        f.seek(0)
        if _is_ascii_stl(head):
            return _parse_ascii_stl(path)
        return _parse_binary_stl(f, path.stat().st_size)


def _parse_binary_stl(f, file_size: int) -> np.ndarray:
    if file_size < 84:
        raise ValueError(f"binary STL too short ({file_size} B; need at least 84)")
    f.seek(80)
    (count,) = struct.unpack("<I", f.read(4))
    expected = 84 + 50 * count
    if expected != file_size:
        # Could be a misnamed ASCII STL; fall back
        f.seek(0)
        if _is_ascii_stl(f.read(1024)):
            return _parse_ascii_stl(Path(f.name))
        raise ValueError(
            f"binary STL size mismatch: header says {count} tris (expects {expected} B), "
            f"file is {file_size} B"
        )
    buf = f.read(50 * count)
    dt = np.dtype([
        ("normal", "<f4", 3),
        ("v0", "<f4", 3),
        ("v1", "<f4", 3),
        ("v2", "<f4", 3),
        ("attr", "<u2"),
    ])
    arr = np.frombuffer(buf, dtype=dt, count=count)
    # np.stack of <f4 inputs already gives float32; no astype needed.
    return np.stack([arr["v0"], arr["v1"], arr["v2"]], axis=1)


def _parse_ascii_stl(path: Path) -> np.ndarray:
    verts: list[list[float]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if s.startswith("vertex "):
                verts.append([float(x) for x in s.split()[1:4]])
    if len(verts) % 3 != 0:
        raise ValueError(f"ASCII STL vertex count {len(verts)} not divisible by 3")
    return np.asarray(verts, dtype=np.float32).reshape(-1, 3, 3)


# ---------- rendering ----------

# Isometric-ish rotation matrix: rotate -30° about X, then -45° about Z.
# Camera looks down +Y after rotation; we drop Y for depth.
def _iso_rotation() -> np.ndarray:
    rx, rz = np.deg2rad(-30.0), np.deg2rad(-45.0)
    Rx = np.array([
        [1, 0, 0],
        [0, np.cos(rx), -np.sin(rx)],
        [0, np.sin(rx), np.cos(rx)],
    ], dtype=np.float32)
    Rz = np.array([
        [np.cos(rz), -np.sin(rz), 0],
        [np.sin(rz), np.cos(rz), 0],
        [0, 0, 1],
    ], dtype=np.float32)
    return Rx @ Rz


def render(tris: np.ndarray, width: int, height: int,
           bg=(245, 245, 245), pad: float = 0.08) -> Image.Image:
    """Render an isometric thumbnail of the triangle mesh."""
    if tris.shape[0] == 0:
        return Image.new("RGB", (width, height), bg)

    R = _iso_rotation()
    rotated = tris @ R.T  # (N, 3, 3)
    # Screen coords: X→horizontal, Z→vertical. Depth = Y after rotation.
    xs = rotated[..., 0]
    ys = rotated[..., 2]  # use rotated Z as screen Y
    depth = rotated[..., 1].mean(axis=1)  # one depth value per triangle

    # Fit bounding box into the image with padding.
    xmin, xmax = float(xs.min()), float(xs.max())
    ymin, ymax = float(ys.min()), float(ys.max())
    obj_w = max(xmax - xmin, 1e-6)
    obj_h = max(ymax - ymin, 1e-6)
    avail_w = width * (1 - 2 * pad)
    avail_h = height * (1 - 2 * pad)
    scale = min(avail_w / obj_w, avail_h / obj_h)
    cx_obj, cy_obj = (xmin + xmax) / 2, (ymin + ymax) / 2
    cx_img, cy_img = width / 2, height / 2

    screen = np.empty((tris.shape[0], 3, 2), dtype=np.float32)
    screen[..., 0] = (xs - cx_obj) * scale + cx_img
    # PIL Y axis grows downward; flip Z so "up" in model = up in image.
    screen[..., 1] = cy_img - (ys - cy_obj) * scale

    # Per-triangle normal (cross of two edges); for Lambertian shading.
    e1 = rotated[:, 1] - rotated[:, 0]
    e2 = rotated[:, 2] - rotated[:, 0]
    normals = np.cross(e1, e2)
    norms = np.linalg.norm(normals, axis=1, keepdims=True)
    norms[norms < 1e-9] = 1.0
    normals = normals / norms

    # Light from upper-front-right.
    light = np.array([0.3, -0.6, 0.8], dtype=np.float32)
    light = light / np.linalg.norm(light)
    intensity = np.clip(np.abs(normals @ light), 0.15, 1.0)  # |dot| so back-faces still shade

    # Painter's algorithm: draw back-to-front (largest Y = farthest from camera).
    order = np.argsort(-depth)

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    for idx in order:
        i = intensity[idx]
        shade = (
            int(round(60 + 150 * i)),
            int(round(110 + 130 * i)),
            int(round(160 + 90 * i)),
        )
        poly = [(float(screen[idx, k, 0]), float(screen[idx, k, 1])) for k in range(3)]
        draw.polygon(poly, fill=shade, outline=None)
    return img


# ---------- G-code splicing ----------

# Match an existing `; thumbnail begin ... ; thumbnail end` block (any width).
_BLOCK_RE = re.compile(
    r"^;\s*thumbnail\s+begin\b.*?^;\s*thumbnail\s+end\s*$\n?",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)


def encode_thumbnail_block(img: Image.Image) -> str:
    """Encode `img` as a PrusaSlicer/Orca thumbnail block string."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()
    b64 = base64.b64encode(png_bytes).decode("ascii")
    # PrusaSlicer convention: third number = length of base64 string (no whitespace).
    header = f"; thumbnail begin {img.width}x{img.height} {len(b64)}\n"
    chunks = [b64[i:i + 78] for i in range(0, len(b64), 78)]
    body = "".join(f"; {chunk}\n" for chunk in chunks)
    return f";\n{header}{body}; thumbnail end\n;\n"


def splice_blocks(gcode_text: str, blocks: Iterable[str]) -> str:
    """Insert/replace thumbnail blocks in `gcode_text`. Idempotent."""
    # Strip any existing thumbnail blocks first.
    cleaned = _BLOCK_RE.sub("", gcode_text)
    payload = "".join(blocks)

    # Inject before first meaningful G-code marker. Order of preference:
    #   1) before `; HEADER_BLOCK_START` (Orca convention)
    #   2) after the leading `; generated by` line if present
    #   3) at the very top
    marker = "; HEADER_BLOCK_START"
    idx = cleaned.find(marker)
    if idx != -1:
        return cleaned[:idx] + payload + cleaned[idx:]
    lines = cleaned.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines):
        if line.lstrip().lower().startswith("; generated by"):
            insert_at = i + 1
            break
    return "".join(lines[:insert_at]) + payload + "".join(lines[insert_at:])


# ---------- CLI ----------

def _parse_sizes(spec: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "x" not in chunk.lower():
            raise ValueError(f"size {chunk!r} must look like 300x300")
        w, h = chunk.lower().split("x", 1)
        out.append((int(w), int(h)))
    if not out:
        raise ValueError("no sizes parsed")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stl", type=Path, required=True, help="Source STL (binary or ASCII).")
    ap.add_argument("--gcode", type=Path, required=True, help="G-code to inject into.")
    ap.add_argument("--sizes", default="48x48,300x300",
                    help="Comma-separated WxH list. Default: 48x48,300x300 (Snapmaker U1 default).")
    ap.add_argument("--in-place", action="store_true",
                    help="Overwrite the G-code in place (default: write to <gcode>.thumb.gcode).")
    ap.add_argument("--output", type=Path, default=None,
                    help="Explicit output path (overrides --in-place behaviour).")
    args = ap.parse_args(argv)

    if not args.stl.exists():
        print(f"STL not found: {args.stl}", file=sys.stderr)
        return 2
    if not args.gcode.exists():
        print(f"G-code not found: {args.gcode}", file=sys.stderr)
        return 2

    try:
        sizes = _parse_sizes(args.sizes)
    except ValueError as e:
        print(f"--sizes: {e}", file=sys.stderr)
        return 2

    tris = parse_stl(args.stl)
    if tris.shape[0] == 0:
        print(f"STL has no triangles: {args.stl}", file=sys.stderr)
        return 3

    blocks = [encode_thumbnail_block(render(tris, w, h)) for w, h in sizes]

    gcode_text = args.gcode.read_text(encoding="utf-8", errors="replace")
    new_text = splice_blocks(gcode_text, blocks)

    if args.output is not None:
        out_path = args.output
    elif args.in_place:
        out_path = args.gcode
    else:
        # `<name>.thumb.gcode` reads better than `<name>.gcode.thumb`
        out_path = args.gcode.with_name(args.gcode.stem + ".thumb" + args.gcode.suffix)

    out_path.write_text(new_text, encoding="utf-8")
    print(f"Injected {len(sizes)} thumbnail block(s): "
          f"{', '.join(f'{w}x{h}' for w, h in sizes)} → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
