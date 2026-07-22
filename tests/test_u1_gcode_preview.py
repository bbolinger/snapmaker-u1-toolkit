"""Toolpath truth preview (u1_gcode_preview): parse, project, color, render.

The 3D review image draws the real toolpaths from the sliced gcode, so
supports are visible, the print pose matches what the printer will run, and
nothing is re-sliced to make the picture. These cover the gcode parse (M486
instance attribution, ;TYPE: category mapping, arcs, metadata), the
isometric projection's z direction (the first cut drew prints upside down),
and the shared part-to-color map the footprint renderer also uses.
"""
from __future__ import annotations

from pathlib import Path

import u1_gcode_preview as gp


def _gcode(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "plate.gcode"
    p.write_text(body)
    return p


_TWO_PART_GCODE = """\
; total filament used [g] = 12.34
; estimated printing time (normal mode) = 1h 2m 3s
M486 S0
M486 Aobj_1_left.stl_id_0_copy_0
M486 S1
M486 Aobj_2_right.stl_id_1_copy_0
M486 S-1
M486 S0
G1 X0 Y0 F3000
G1 Z0.2
;TYPE:Brim
G1 X10 Y0 E1
;TYPE:Outer wall
G1 X10 Y10 E1
;TYPE:Support
G1 X0 Y10 E1
;TYPE:Sparse infill
G1 X5 Y5 E1
G1 Z5.0
;TYPE:Top surface
G1 X0 Y0 E1
M486 S-1
M486 S1
G1 X100 Y100 F3000
G1 Z0.2
;TYPE:Outer wall
G1 X110 Y100 E1
G1 X110 Y110 E1
M486 S-1
"""


# ---------- parse ----------

def test_parse_maps_types_and_keeps_full_instance_names(tmp_path):
    parsed = gp.parse_toolpaths(_gcode(tmp_path, _TWO_PART_GCODE))
    cats = [s[2] for s in parsed["segments"]]
    assert "brim" in cats and "support" in cats and "top" in cats
    assert "outer" in cats
    # Sparse infill is interior noise at preview scale and must be dropped.
    assert len(parsed["segments"]) == 6
    # Full M486 instance names, exactly what the footprint renderer keys on.
    assert parsed["parts"] == ["obj_1_left.stl_id_0_copy_0",
                               "obj_2_right.stl_id_1_copy_0"]


def test_parse_reads_filament_and_time_metadata(tmp_path):
    meta = gp.parse_toolpaths(_gcode(tmp_path, _TWO_PART_GCODE))["meta"]
    assert meta["filament_g"] == "12.34"
    assert meta["time"] == "1h 2m 3s"


def test_parse_attributes_segments_to_the_active_part(tmp_path):
    parsed = gp.parse_toolpaths(_gcode(tmp_path, _TWO_PART_GCODE))
    by_part: dict = {}
    for _z, _seq, _cat, part, *_ in parsed["segments"]:
        by_part.setdefault(part, 0)
        by_part[part] += 1
    assert by_part["obj_1_left.stl_id_0_copy_0"] == 4
    assert by_part["obj_2_right.stl_id_1_copy_0"] == 2


def test_parse_flattens_arcs_to_chords(tmp_path):
    body = """\
M486 S0
M486 Aobj_1_arc.stl_id_0_copy_0
M486 S-1
M486 S0
G1 X10 Y0 F3000
G1 Z0.2
;TYPE:Outer wall
G3 X0 Y10 I-10 J0 E1
M486 S-1
"""
    parsed = gp.parse_toolpaths(_gcode(tmp_path, body))
    # A quarter arc of r=10 must land as multiple chords, not one line.
    assert len(parsed["segments"]) > 3
    end = parsed["segments"][-1]
    assert abs(end[6] - 0) < 0.5 and abs(end[7] - 10) < 0.5


def test_travel_and_untyped_moves_produce_no_segments(tmp_path):
    body = """\
M486 S0
M486 Aobj_1_x.stl_id_0_copy_0
M486 S-1
M486 S0
G1 X0 Y0 F3000
G1 X50 Y50
G1 X60 Y60 E1
M486 S-1
"""
    # The E move has no ;TYPE: label yet, so it stays uncategorized.
    assert gp.parse_toolpaths(_gcode(tmp_path, body))["segments"] == []


# ---------- projection ----------

def test_iso_projection_z_raises_points():
    """Higher z must raise a point on screen. The first cut subtracted z and
    drew every print upside down; the operator caught it on review."""
    _u0, v0 = gp._iso(50, 50, 0)
    _u1, v1 = gp._iso(50, 50, 30)
    assert v1 > v0


def test_iso_projection_depth_raises_points():
    """Farther from the viewer corner (larger x + y) also raises a point,
    which is what makes the bed grid read as receding."""
    _u0, v0 = gp._iso(0, 0, 0)
    _u1, v1 = gp._iso(100, 100, 0)
    assert v1 > v0


# ---------- shared colors ----------

def test_part_colors_matches_the_footprint_formula():
    import colorsys
    names = ["b_part", "a_part", "c_part"]
    colors = gp.part_colors(names)
    ordered = sorted(names)
    for i, name in enumerate(ordered):
        r, g, b = colorsys.hsv_to_rgb(i / len(ordered), 0.55, 0.9)
        assert colors[name] == (int(r * 255), int(g * 255), int(b * 255))


def test_part_colors_is_order_independent():
    a = gp.part_colors(["x_1", "y_2", "z_3"])
    b = gp.part_colors(["z_3", "x_1", "y_2"])
    assert a == b


# ---------- render ----------

def test_render_writes_png_and_reports_supports(tmp_path):
    out = tmp_path / "iso.png"
    res = gp.render_iso_preview(_gcode(tmp_path, _TWO_PART_GCODE), out)
    assert res["ok"], res
    assert out.exists() and out.stat().st_size > 0
    assert res["support_segments"] == 1
    assert res["meta"]["filament_g"] == "12.34"
    assert res["parts"] == ["obj_1_left.stl_id_0_copy_0",
                            "obj_2_right.stl_id_1_copy_0"]


def test_render_fails_soft_on_empty_gcode(tmp_path):
    out = tmp_path / "iso.png"
    res = gp.render_iso_preview(_gcode(tmp_path, "; nothing here\n"), out)
    assert not res["ok"]
    assert not out.exists()


def test_chrome_free_render_for_thumbnails(tmp_path):
    """chrome=False drops the header and footer text: the printer
    touchscreen shows this at 48 and 300 px, where text is just noise."""
    out = tmp_path / "thumb.png"
    res = gp.render_iso_preview(_gcode(tmp_path, _TWO_PART_GCODE), out,
                                canvas_px=600, chrome=False,
                                title="ignored when chrome is off")
    assert res["ok"], res
    assert out.exists() and out.stat().st_size > 0
    from PIL import Image
    assert Image.open(out).size == (600, 600)


def test_top_view_renders_full_bed_with_shared_styling(tmp_path):
    """The top-down placement view draws the same toolpaths with the same
    part colors as the 3D view, over the full bed."""
    out = tmp_path / "top.png"
    res = gp.render_top_preview(_gcode(tmp_path, _TWO_PART_GCODE), out,
                                title="Plate 1 of 1",
                                label_below="2 parts, T0 PETG")
    assert res["ok"], res
    assert res["part_count"] == 2
    assert res["support_segments"] == 1
    assert out.exists() and out.stat().st_size > 0


def test_top_view_fails_soft_on_empty_gcode(tmp_path):
    res = gp.render_top_preview(_gcode(tmp_path, "; nothing\n"),
                                tmp_path / "top.png")
    assert not res["ok"]
