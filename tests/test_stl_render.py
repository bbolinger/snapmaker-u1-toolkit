"""Tests for tools/_stl_render.py — geometry helpers + view rotations
+ rendering primitives. These cover the math the orientation renderer
and the thumbnail injector both depend on."""
from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

import pytest

pytest.importorskip("PIL")
pytest.importorskip("numpy")

import numpy as np
from PIL import Image

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import _stl_render as r  # noqa: E402


# ---------- fixtures ----------

def _binary_cube(side: float = 10.0) -> bytes:
    """12-triangle axis-aligned cube from (0,0,0) to (side,side,side)."""
    s = side
    pts = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
           (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [
        (0, 1, 2), (0, 2, 3),  # bottom (Z=0, normal Z=-1 → overhang)
        (4, 6, 5), (4, 7, 6),  # top (Z=s, normal Z=+1)
        (0, 4, 5), (0, 5, 1),  # front (Y=0)
        (2, 6, 7), (2, 7, 3),  # back (Y=s)
        (1, 5, 6), (1, 6, 2),  # right (X=s)
        (0, 3, 7), (0, 7, 4),  # left (X=0)
    ]
    buf = io.BytesIO()
    buf.write(b"\x00" * 80)
    buf.write(struct.pack("<I", len(faces)))
    for a, b, c in faces:
        buf.write(struct.pack("<3f", 0, 0, 0))  # normal placeholder; render recomputes
        for vi in (a, b, c):
            buf.write(struct.pack("<3f", *pts[vi]))
        buf.write(struct.pack("<H", 0))
    return buf.getvalue()


@pytest.fixture
def cube_tris(tmp_path):
    p = tmp_path / "cube.stl"
    p.write_bytes(_binary_cube(side=20.0))
    return r.parse_stl(p)


# ---------- bbox ----------

def test_bbox_returns_axis_aligned_min_max(cube_tris):
    xmin, xmax, ymin, ymax, zmin, zmax = r.bbox(cube_tris)
    assert xmin == 0.0 and xmax == 20.0
    assert ymin == 0.0 and ymax == 20.0
    assert zmin == 0.0 and zmax == 20.0


def test_bbox_on_empty_returns_zeros():
    assert r.bbox(np.zeros((0, 3, 3), dtype=np.float32)) == (0,) * 6


# ---------- face_normals ----------

def test_face_normals_are_unit_length(cube_tris):
    n = r.face_normals(cube_tris)
    assert n.shape == (12, 3)
    norms = np.linalg.norm(n, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_face_normals_handle_degenerate_triangles_without_nan():
    """Zero-area triangle (collinear verts) — normal goes to zero, no NaN."""
    degenerate = np.array([[[0, 0, 0], [1, 0, 0], [2, 0, 0]]], dtype=np.float32)
    n = r.face_normals(degenerate)
    assert not np.isnan(n).any()


# ---------- overhang_mask ----------

def test_overhang_mask_flags_bottom_face_of_cube(cube_tris):
    """Cube bottom (Z=0, normal pointing down → -1) is the canonical overhang.
    Top (Z=20, normal +1) and sides (normal Z=0) are not."""
    mask = r.overhang_mask(cube_tris, threshold=-0.3)
    assert mask.shape == (12,)
    # Exactly 2 triangles make up the bottom face
    assert int(mask.sum()) == 2, f"expected 2 overhang faces (cube bottom), got {int(mask.sum())}"


def test_overhang_mask_threshold_picks_only_downward_face():
    """Three triangles: one facing down (normal Z=-1), one up (+1), one sideways (0).
    Only the strictly-downward one should be flagged with a negative threshold.
    Winding order matters: counter-clockwise from the normal's POV → normal points
    that way (right-hand rule)."""
    tris = np.array([
        # facing DOWN: clockwise from above → normal points -Z
        [[0, 0, 0], [0, 1, 0], [1, 0, 0]],
        # facing UP: counter-clockwise from above → normal +Z
        [[0, 0, 1], [1, 0, 1], [0, 1, 1]],
        # facing SIDEWAYS (+X direction): normal Z = 0
        [[0, 0, 0], [0, 0, 1], [0, 1, 0]],
    ], dtype=np.float32)
    mask = r.overhang_mask(tris, threshold=-0.5)
    assert mask.tolist() == [True, False, False], \
        f"only the downward triangle should flag; normals were {r.face_normals(tris).tolist()}"
    # threshold=0 is exclusive (`< 0` not `<= 0`); face with normal exactly 0 won't flag
    mask_zero = r.overhang_mask(tris, threshold=0.0)
    assert mask_zero.tolist() == [True, False, False]  # downward (Z=-1) still <0


def test_overhang_mask_empty_mesh_returns_empty():
    mask = r.overhang_mask(np.zeros((0, 3, 3), dtype=np.float32))
    assert mask.shape == (0,)
    assert mask.dtype == bool


# ---------- view_rotation ----------

def test_view_rotation_known_views_all_valid():
    """Every documented view name resolves to a 3x3 matrix."""
    for view in ("iso", "front", "side", "top"):
        R = r.view_rotation(view)
        assert R.shape == (3, 3)
        # Rotation matrices preserve lengths: det == ±1
        assert abs(abs(np.linalg.det(R)) - 1.0) < 1e-5, f"{view}: not a rotation"


def test_view_rotation_unknown_raises():
    with pytest.raises(ValueError, match="unknown view"):
        r.view_rotation("diagonal")


def test_front_view_is_identity():
    """Front camera = no rotation needed (our convention is camera-down-+Y)."""
    R = r.view_rotation("front")
    assert np.allclose(R, np.eye(3), atol=1e-6)


# ---------- render_view ----------

def test_render_view_produces_non_blank_for_each_view(cube_tris):
    """Each of the 4 views renders a cube as something other than the bg color."""
    for view in ("iso", "front", "side", "top"):
        img = r.render_view(cube_tris, 64, 64, view=view)
        arr = np.array(img)
        bg = arr[0, 0]
        differ_pct = float(np.any(arr != bg, axis=-1).mean())
        assert differ_pct > 0.05, f"{view}: render too blank ({differ_pct:.1%} differ from bg)"


def test_render_view_overhang_flags_paint_orange():
    """When overhang_flags is set, flagged triangles render in the overhang
    color regardless of shading. Use a single unoccluded downward-facing
    triangle so painter's algorithm can't hide it (the cube test setup hides
    the bottom face behind the front/top/right — correctly!)."""
    # One downward-facing triangle, no occlusion
    tris = np.array([[[0, 0, 5], [10, 0, 5], [5, 10, 5]]], dtype=np.float32)
    # Clockwise from above = normal points down. Verify:
    tris_clockwise = np.array([[[0, 0, 5], [5, 10, 5], [10, 0, 5]]], dtype=np.float32)
    n = r.face_normals(tris_clockwise)
    assert n[0, 2] < -0.9, f"test setup bug: expected -Z normal, got {n[0].tolist()}"

    mask = np.array([True])
    img = r.render_view(tris_clockwise, 128, 128, view="top",
                        overhang_flags=mask,
                        overhang_color=(255, 0, 255))  # magenta — distinct from defaults
    arr = np.array(img)
    has_overhang_color = int(np.all(arr == (255, 0, 255), axis=-1).sum())
    assert has_overhang_color > 0, \
        f"no overhang pixels rendered despite mask (got 0; image is {arr[64,64].tolist()})"


def test_render_view_empty_mesh_returns_blank():
    img = r.render_view(np.zeros((0, 3, 3), dtype=np.float32), 32, 32, view="iso")
    assert img.size == (32, 32)


# ---------- legacy render() alias ----------

def test_render_alias_matches_iso_view(cube_tris):
    """Legacy single-view `render()` should produce the same output as
    `render_view(view='iso')` with the same args — same alias contract
    gcode_inject_thumbnail.py depends on."""
    a = r.render(cube_tris, 96, 96)
    b = r.render_view(cube_tris, 96, 96, view="iso")
    assert np.array_equal(np.array(a), np.array(b))
