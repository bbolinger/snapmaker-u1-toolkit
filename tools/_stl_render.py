"""Shared STL parsing + projection + Lambertian rendering for the toolkit's
PIL-backed tools (`gcode_inject_thumbnail.py`, `render_stl_orientation.py`).

Pure render primitives — no CLI, no file naming, no project-specific defaults.

Dependencies: numpy + PIL. The toolkit's safety scripts intentionally avoid
both; tools that opt into 3D rendering live here.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


# ============================================================
# STL parsing
# ============================================================

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


# ============================================================
# Geometry helpers
# ============================================================

def bbox(tris: np.ndarray) -> tuple[float, float, float, float, float, float]:
    """Return (xmin, xmax, ymin, ymax, zmin, zmax) over all vertices."""
    if tris.shape[0] == 0:
        return (0.0,) * 6
    flat = tris.reshape(-1, 3)
    mn = flat.min(axis=0)
    mx = flat.max(axis=0)
    return float(mn[0]), float(mx[0]), float(mn[1]), float(mx[1]), float(mn[2]), float(mx[2])


def face_normals(tris: np.ndarray) -> np.ndarray:
    """Per-triangle unit normal vectors (N, 3). Zero-area faces get a zero normal."""
    if tris.shape[0] == 0:
        return np.zeros((0, 3), dtype=np.float32)
    e1 = tris[:, 1] - tris[:, 0]
    e2 = tris[:, 2] - tris[:, 0]
    normals = np.cross(e1, e2)
    norms = np.linalg.norm(normals, axis=1, keepdims=True)
    norms[norms < 1e-9] = 1.0
    return normals / norms


def face_areas(tris: np.ndarray) -> np.ndarray:
    """Per-triangle surface area (N,) in the mesh's native units²
    (mm² for printer-bound STLs). Zero-area faces report 0."""
    if tris.shape[0] == 0:
        return np.zeros((0,), dtype=np.float32)
    e1 = tris[:, 1] - tris[:, 0]
    e2 = tris[:, 2] - tris[:, 0]
    return 0.5 * np.linalg.norm(np.cross(e1, e2), axis=1)


def overhang_mask(tris: np.ndarray, threshold: float = -0.3) -> np.ndarray:
    """Boolean mask flagging triangles whose face normal Z is below the
    threshold — these are the downward-pointing faces a 3D printer can't
    print without support material.

    threshold=-0.3 ≈ slopes more than 17° below horizontal (a common
    operator-tunable overhang threshold). Tighter (closer to 0) = more
    paranoid; looser (more negative) = fewer faces flagged."""
    if tris.shape[0] == 0:
        return np.zeros((0,), dtype=bool)
    return face_normals(tris)[:, 2] < threshold


# ============================================================
# View rotations (right-handed; printer convention Z=up)
# ============================================================

def _rot_x(deg: float) -> np.ndarray:
    r = np.deg2rad(deg)
    c, s = np.cos(r), np.sin(r)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float32)


def _rot_z(deg: float) -> np.ndarray:
    r = np.deg2rad(deg)
    c, s = np.cos(r), np.sin(r)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float32)


def view_rotation(view: str) -> np.ndarray:
    """Return the rotation matrix for a named camera view.

    Conventions (printer-native, Z=up):
    - screen X    = rotated[0]
    - screen Y    = rotated[2]  (flipped at render-time so +Z renders up)
    - depth       = rotated[1]  (painter's ordering; larger Y = farther)

    Views:
    - 'iso'   : isometric (-30° about X, then -45° about Z)
    - 'front' : look from -Y toward +Y — rectangular silhouette, Z up
    - 'side'  : look from -X toward +X — rectangular silhouette, Z up
    - 'top'   : look from +Z down — bed footprint (X horizontal, Y up-on-screen)
    """
    if view == "iso":
        return _rot_x(-30.0) @ _rot_z(-45.0)
    if view == "front":
        # Camera already looks down +Y in our convention. Identity rotation.
        return np.eye(3, dtype=np.float32)
    if view == "side":
        # Rotate -90° around Z so the model's +X axis ends up where the camera
        # is looking. After: rotated[0]=Y, rotated[1]=-X, rotated[2]=Z — so
        # screen X = model Y, screen up = model Z, depth = -model X.
        return _rot_z(-90.0)
    if view == "top":
        # Look straight down: rotate +90° around X so model +Z faces -Y (the
        # camera direction). After: rotated[0]=X, rotated[1]=-Z, rotated[2]=Y —
        # screen X = model X, screen up = model Y, depth = -model Z.
        return _rot_x(90.0)
    raise ValueError(f"unknown view: {view!r}")


# Alias used by gcode_inject_thumbnail; preserves old internal name.
def _iso_rotation() -> np.ndarray:
    return view_rotation("iso")


# ============================================================
# Render
# ============================================================

# Default palette — soft blue model with deeper blue underside.
DEFAULT_MODEL_TOP = (98, 178, 235)
DEFAULT_MODEL_BOTTOM = (30, 82, 125)
DEFAULT_OVERHANG = (255, 135, 62)
DEFAULT_BG = (245, 245, 245)


def _shade(intensity: float, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> tuple[int, int, int]:
    """Blend top and bottom palette colors by `intensity` ∈ [0, 1]."""
    return (
        int(round(bottom[0] + (top[0] - bottom[0]) * intensity)),
        int(round(bottom[1] + (top[1] - bottom[1]) * intensity)),
        int(round(bottom[2] + (top[2] - bottom[2]) * intensity)),
    )


def render_view(
    tris: np.ndarray,
    width: int,
    height: int,
    *,
    view: str = "iso",
    overhang_flags: np.ndarray | None = None,
    bg: tuple[int, int, int] = DEFAULT_BG,
    model_top: tuple[int, int, int] = DEFAULT_MODEL_TOP,
    model_bottom: tuple[int, int, int] = DEFAULT_MODEL_BOTTOM,
    overhang_color: tuple[int, int, int] = DEFAULT_OVERHANG,
    pad: float = 0.08,
) -> Image.Image:
    """Render a triangle mesh from one named view with Lambertian shading +
    painter's algorithm. Optional `overhang_flags` boolean mask paints
    flagged triangles in `overhang_color` regardless of shading."""
    if tris.shape[0] == 0:
        return Image.new("RGB", (width, height), bg)

    R = view_rotation(view)
    rotated = tris @ R.T  # (N, 3, 3)

    # Screen coords: X→horizontal, rotated-Z→vertical, rotated-Y→depth.
    xs = rotated[..., 0]
    ys = rotated[..., 2]
    depth = rotated[..., 1].mean(axis=1)

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
    screen[..., 1] = cy_img - (ys - cy_obj) * scale

    # Per-triangle normal under the rotated frame for shading.
    e1 = rotated[:, 1] - rotated[:, 0]
    e2 = rotated[:, 2] - rotated[:, 0]
    normals = np.cross(e1, e2)
    norms = np.linalg.norm(normals, axis=1, keepdims=True)
    norms[norms < 1e-9] = 1.0
    normals = normals / norms

    light = np.array([0.3, -0.6, 0.8], dtype=np.float32)
    light = light / np.linalg.norm(light)
    intensity = np.clip(np.abs(normals @ light), 0.15, 1.0)

    order = np.argsort(-depth)

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    for idx in order:
        if overhang_flags is not None and overhang_flags[idx]:
            shade = overhang_color
        else:
            shade = _shade(float(intensity[idx]), model_top, model_bottom)
        poly = [(float(screen[idx, k, 0]), float(screen[idx, k, 1])) for k in range(3)]
        draw.polygon(poly, fill=shade, outline=None)
    return img


# ---------- legacy alias for gcode_inject_thumbnail.py ----------

def render(tris: np.ndarray, width: int, height: int,
           bg: tuple[int, int, int] = DEFAULT_BG, pad: float = 0.08) -> Image.Image:
    """Backward-compatible single-view ('iso') render. Used by the
    thumbnail injector which only ever wants one view."""
    return render_view(tris, width, height, view="iso", bg=bg, pad=pad)
