"""Tests for tools/gcode_inject_thumbnail.py.

Critical invariants:
- Binary + ASCII STL both parse to the same triangle count
- Render produces a non-blank PNG (rules out empty-mesh edge cases)
- Splice format matches PrusaSlicer/Orca convention so Moonraker/Snapmaker
  parse it as a thumbnail
- Re-running on a G-code that already has thumbnails REPLACES them, not
  stacks them (idempotence)
- Inject location respects `; HEADER_BLOCK_START` when present
"""
from __future__ import annotations

import base64
import io
import re
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

import gcode_inject_thumbnail as inj  # noqa: E402


# ---------- helpers ----------

def _binary_stl_cube() -> bytes:
    """12 triangles forming a 10mm cube centered at origin."""
    pts = [(-5, -5, -5), (5, -5, -5), (5, 5, -5), (-5, 5, -5),
           (-5, -5, 5), (5, -5, 5), (5, 5, 5), (-5, 5, 5)]
    faces = [
        (0, 1, 2), (0, 2, 3),  # bottom
        (4, 6, 5), (4, 7, 6),  # top
        (0, 4, 5), (0, 5, 1),  # front
        (2, 6, 7), (2, 7, 3),  # back
        (1, 5, 6), (1, 6, 2),  # right
        (0, 3, 7), (0, 7, 4),  # left
    ]
    buf = io.BytesIO()
    buf.write(b"\x00" * 80)
    buf.write(struct.pack("<I", len(faces)))
    for a, b, c in faces:
        buf.write(struct.pack("<3f", 0, 0, 0))  # normal (unused by our renderer)
        for vi in (a, b, c):
            buf.write(struct.pack("<3f", *pts[vi]))
        buf.write(struct.pack("<H", 0))
    return buf.getvalue()


def _ascii_stl_triangle() -> str:
    return (
        "solid test\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        "      vertex 10 0 0\n"
        "      vertex 5 10 0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid test\n"
    )


# ---------- STL parsing ----------

def test_parse_binary_stl_cube(tmp_path):
    stl = tmp_path / "cube.stl"
    stl.write_bytes(_binary_stl_cube())
    tris = inj.parse_stl(stl)
    assert tris.shape == (12, 3, 3)
    assert tris.dtype == np.float32


def test_parse_ascii_stl_triangle(tmp_path):
    stl = tmp_path / "tri.stl"
    stl.write_text(_ascii_stl_triangle())
    tris = inj.parse_stl(stl)
    assert tris.shape == (1, 3, 3)


def test_parse_binary_stl_rejects_bad_size(tmp_path):
    """A binary header claiming N triangles must match the file size."""
    stl = tmp_path / "bad.stl"
    stl.write_bytes(b"\x00" * 80 + struct.pack("<I", 100) + b"\x00" * 10)
    with pytest.raises(ValueError, match="size mismatch"):
        inj.parse_stl(stl)


# ---------- render ----------

def test_render_produces_non_blank_image(tmp_path):
    stl = tmp_path / "cube.stl"
    stl.write_bytes(_binary_stl_cube())
    tris = inj.parse_stl(stl)
    img = inj.render(tris, 64, 64)
    assert img.size == (64, 64)
    arr = np.array(img)
    bg = arr[0, 0]  # corner is background
    diff = np.any(arr != bg, axis=-1)
    # At least 5% of pixels should differ from the corner — i.e. the object renders.
    assert diff.mean() > 0.05, f"render is mostly blank ({diff.mean():.3%} differ)"


def test_render_empty_mesh_returns_blank(tmp_path):
    empty = np.zeros((0, 3, 3), dtype=np.float32)
    img = inj.render(empty, 32, 32)
    assert img.size == (32, 32)


# ---------- encode + splice ----------

def test_encode_block_has_prusaslicer_shape(tmp_path):
    stl = tmp_path / "cube.stl"
    stl.write_bytes(_binary_stl_cube())
    img = inj.render(inj.parse_stl(stl), 48, 48)
    block = inj.encode_thumbnail_block(img)
    assert block.startswith(";\n; thumbnail begin 48x48 ")
    # Block is sandwiched between leading and trailing `;\n` close-comment markers
    assert "; thumbnail end\n" in block
    assert block.endswith(";\n")
    # All payload lines start with `; `
    payload_lines = [ln for ln in block.splitlines() if ln and ln != ";"
                     and not ln.startswith("; thumbnail")]
    for ln in payload_lines:
        assert ln.startswith("; "), f"payload line missing `; ` prefix: {ln!r}"


def test_encode_block_byte_count_matches_base64_length(tmp_path):
    """Moonraker validates the 3rd number in `; thumbnail begin WxH N` against
    the actual base64 length. Get it wrong and the thumbnail won't parse."""
    stl = tmp_path / "cube.stl"
    stl.write_bytes(_binary_stl_cube())
    img = inj.render(inj.parse_stl(stl), 48, 48)
    block = inj.encode_thumbnail_block(img)
    m = re.match(r";\n; thumbnail begin \d+x\d+ (\d+)\n", block)
    assert m is not None
    declared = int(m.group(1))
    # Reconstruct base64 from payload lines
    b64 = "".join(
        ln[2:] for ln in block.splitlines()
        if ln.startswith("; ") and not ln.startswith("; thumbnail")
    )
    assert len(b64) == declared
    # And the base64 actually decodes to a PNG
    decoded = base64.b64decode(b64)
    assert decoded[:8] == b"\x89PNG\r\n\x1a\n"


def test_splice_injects_before_header_block_start():
    gcode = (
        "; generated by Snapmaker Orca 2.3.4\n"
        ";\n"
        "; HEADER_BLOCK_START\n"
        "; HEADER_BLOCK_END\n"
        "G28\n"
    )
    block = "; thumbnail begin 48x48 4\n; ABCD\n; thumbnail end\n"
    result = inj.splice_blocks(gcode, [block])
    assert block in result
    # Block must appear BEFORE `; HEADER_BLOCK_START`
    assert result.find(block) < result.find("; HEADER_BLOCK_START")


def test_splice_is_idempotent_replaces_existing_blocks():
    """Running the inject twice must not stack thumbnails — the second pass
    replaces, not appends."""
    gcode = (
        "; generated by Snapmaker Orca 2.3.4\n"
        "; HEADER_BLOCK_START\n"
        "G28\n"
    )
    block_a = ";\n; thumbnail begin 48x48 4\n; AAAA\n; thumbnail end\n;\n"
    block_b = ";\n; thumbnail begin 48x48 4\n; BBBB\n; thumbnail end\n;\n"
    once = inj.splice_blocks(gcode, [block_a])
    twice = inj.splice_blocks(once, [block_b])
    assert "AAAA" not in twice
    assert "BBBB" in twice
    # Exactly one begin block
    assert twice.count("thumbnail begin") == 1


def test_splice_falls_back_to_top_when_no_header(tmp_path):
    gcode = "G28\nG1 X1 Y1\n"
    block = ";\n; thumbnail begin 48x48 4\n; ZZZZ\n; thumbnail end\n;\n"
    result = inj.splice_blocks(gcode, [block])
    assert result.startswith(block)


def test_splice_falls_back_to_after_generator_line():
    """When no `; HEADER_BLOCK_START` marker, insert AFTER `; generated by`."""
    gcode = (
        "; generated by SomeOtherSlicer 1.0\n"
        "G28\n"
        "G1 X1 Y1\n"
    )
    block = ";\n; thumbnail begin 48x48 4\n; XXXX\n; thumbnail end\n;\n"
    result = inj.splice_blocks(gcode, [block])
    lines = result.splitlines()
    # generator line stays at top; block injected starting on line 2
    assert lines[0] == "; generated by SomeOtherSlicer 1.0"
    assert lines[1] == ";"
    assert "; thumbnail begin 48x48 4" in lines[2]
    # And G28 still comes after the block
    g28_idx = next(i for i, line in enumerate(lines) if line == "G28")
    thumb_idx = next(i for i, line in enumerate(lines) if "thumbnail begin" in line)
    assert g28_idx > thumb_idx


def test_parse_binary_stl_rejects_too_short(tmp_path):
    """A 10-byte file is shorter than the 84-byte minimum header — must raise."""
    stl = tmp_path / "tiny.stl"
    stl.write_bytes(b"\x00" * 10)
    with pytest.raises(ValueError, match="too short"):
        inj.parse_stl(stl)


# ---------- main() CLI ----------

def test_main_writes_thumb_gcode_by_default(tmp_path):
    stl = tmp_path / "cube.stl"
    stl.write_bytes(_binary_stl_cube())
    g = tmp_path / "plate_1.gcode"
    g.write_text("; generated by Snapmaker Orca 2.3.4\n; HEADER_BLOCK_START\nG28\n")
    rc = inj.main([
        "--stl", str(stl),
        "--gcode", str(g),
        "--sizes", "48x48",
    ])
    assert rc == 0
    out = tmp_path / "plate_1.thumb.gcode"
    assert out.exists()
    body = out.read_text()
    assert "thumbnail begin 48x48" in body
    assert "thumbnail end" in body
    # original file is untouched
    assert "thumbnail begin" not in g.read_text()


def test_main_in_place_overwrites_gcode(tmp_path):
    stl = tmp_path / "cube.stl"
    stl.write_bytes(_binary_stl_cube())
    g = tmp_path / "plate_1.gcode"
    g.write_text("; generated by Snapmaker Orca 2.3.4\n; HEADER_BLOCK_START\nG28\n")
    rc = inj.main([
        "--stl", str(stl), "--gcode", str(g),
        "--sizes", "48x48,300x300", "--in-place",
    ])
    assert rc == 0
    body = g.read_text()
    assert body.count("thumbnail begin 48x48") == 1
    assert body.count("thumbnail begin 300x300") == 1


def test_main_missing_stl_returns_2(tmp_path):
    g = tmp_path / "x.gcode"
    g.write_text("G28\n")
    rc = inj.main(["--stl", str(tmp_path / "nope.stl"), "--gcode", str(g)])
    assert rc == 2


def test_main_invalid_sizes_returns_2(tmp_path):
    stl = tmp_path / "cube.stl"
    stl.write_bytes(_binary_stl_cube())
    g = tmp_path / "x.gcode"
    g.write_text("G28\n")
    rc = inj.main(["--stl", str(stl), "--gcode", str(g), "--sizes", "not-a-size"])
    assert rc == 2
