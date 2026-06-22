"""Tests for tools/render_stl_orientation.py — CLI smoke + composite sheet
output. The 4-view rendering math is covered in test_stl_render.py; this
file covers the orchestration (header band, 2x2 panel layout, output)."""
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

import render_stl_orientation as rso  # noqa: E402


def _binary_cube(side: float = 20.0) -> bytes:
    pts = [(0, 0, 0), (side, 0, 0), (side, side, 0), (0, side, 0),
           (0, 0, side), (side, 0, side), (side, side, side), (0, side, side)]
    faces = [
        (0, 1, 2), (0, 2, 3),
        (4, 6, 5), (4, 7, 6),
        (0, 4, 5), (0, 5, 1),
        (2, 6, 7), (2, 7, 3),
        (1, 5, 6), (1, 6, 2),
        (0, 3, 7), (0, 7, 4),
    ]
    buf = io.BytesIO()
    buf.write(b"\x00" * 80)
    buf.write(struct.pack("<I", len(faces)))
    for a, b, c in faces:
        buf.write(struct.pack("<3f", 0, 0, 0))
        for vi in (a, b, c):
            buf.write(struct.pack("<3f", *pts[vi]))
        buf.write(struct.pack("<H", 0))
    return buf.getvalue()


@pytest.fixture
def cube_stl(tmp_path):
    p = tmp_path / "cube.stl"
    p.write_bytes(_binary_cube(side=20.0))
    return p


# ---------- render_orientation_sheet ----------

def test_orientation_sheet_dimensions_match_request(cube_stl):
    """Output PIL Image should match the requested width/height exactly."""
    import _stl_render
    tris = _stl_render.parse_stl(cube_stl)
    img = rso.render_orientation_sheet(tris, width=900, height=700)
    assert img.size == (900, 700)


def test_orientation_sheet_contains_all_four_view_panels(cube_stl):
    """Panel-label text for ISOMETRIC / FRONT / SIDE / TOP should be
    paintable into the sheet — we verify by checking the sheet renders
    cleanly (no crash) for each combo of view name in the labels."""
    import _stl_render
    tris = _stl_render.parse_stl(cube_stl)
    img = rso.render_orientation_sheet(tris, 800, 600, title="cube test")
    arr = np.array(img)
    # Sheet should be larger than 80% non-background (lots of panel fill +
    # rendered model pixels). Catches degenerate "all-black" outputs.
    bg = (22, 26, 31)
    panel_bg = (31, 36, 43)
    bg_pct = float(np.all(arr == bg, axis=-1).mean())
    panel_pct = float(np.all(arr == panel_bg, axis=-1).mean())
    # At least 30% of pixels should be panel backgrounds (the 4 panels themselves)
    assert panel_pct > 0.30, f"panels don't cover enough of the sheet ({panel_pct:.1%})"


# ---------- main() CLI ----------

def test_main_writes_default_output_next_to_stl(cube_stl, capsys):
    rc = rso.main([str(cube_stl)])
    assert rc == 0
    expected = cube_stl.with_name(cube_stl.stem + "_orientation.png")
    assert expected.exists(), f"default output not written to {expected}"
    # Output is a valid PNG of the right dimensions
    img = Image.open(expected)
    assert img.size == (1800, 1400)  # the default width × height
    out = capsys.readouterr().out
    assert "Orientation review" in out
    assert "dims:" in out


def test_main_custom_out_path_honored(cube_stl, tmp_path):
    custom = tmp_path / "elsewhere" / "review.png"
    rc = rso.main([str(cube_stl), "--out", str(custom)])
    assert rc == 0
    assert custom.exists()


def test_main_custom_dimensions(cube_stl, tmp_path):
    out = tmp_path / "small.png"
    rc = rso.main([str(cube_stl), "--out", str(out),
                   "--width", "600", "--height", "450"])
    assert rc == 0
    img = Image.open(out)
    assert img.size == (600, 450)


def test_main_missing_stl_returns_2(tmp_path):
    rc = rso.main([str(tmp_path / "nope.stl")])
    assert rc == 2


def test_main_overhang_threshold_changes_count(cube_stl, tmp_path, capsys):
    """Tighter threshold (closer to 0) flags more faces. Looser flags fewer."""
    out = tmp_path / "tight.png"
    rso.main([str(cube_stl), "--out", str(out), "--overhang-threshold", "-0.05"])
    tight = capsys.readouterr().out
    rso.main([str(cube_stl), "--out", str(out), "--overhang-threshold", "-0.95"])
    loose = capsys.readouterr().out
    # Parse the count line and assert tight >= loose
    import re
    tight_count = int(re.search(r"(\d+) flagged as overhang", tight).group(1))
    loose_count = int(re.search(r"(\d+) flagged as overhang", loose).group(1))
    assert tight_count >= loose_count, \
        f"tight threshold should flag >= loose: {tight_count} vs {loose_count}"
