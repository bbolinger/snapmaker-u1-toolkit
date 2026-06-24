from __future__ import annotations
import numpy as np
from pathlib import Path
from u1_orient import write_binary_stl
from render_slice_review import first_layer_bbox, render_slice_review

def test_first_layer_bbox_from_gcode(tmp_path):
    g=tmp_path/'a.gcode'; g.write_text('G1 Z0.2\nG1 X1 Y2 E0.1\nG1 X5 Y6 E0.2\nG1 Z1.0\nG1 X99 Y99 E0.3\n')
    assert first_layer_bbox(g)==(1.0,5.0,2.0,6.0)

def test_render_slice_review_outputs_png(tmp_path):
    stl=tmp_path/'a.stl'; write_binary_stl(stl, np.array([[[0,0,0],[10,0,0],[0,10,1]]], dtype=np.float32))
    g=tmp_path/'a.gcode'; g.write_text('G1 Z0.2\nG1 X0 Y0\nG1 X10 Y10\n')
    out=tmp_path/'review.png'; res=render_slice_review(stl,out,gcode=g)
    assert out.exists() and out.stat().st_size > 1000
    assert res['first_layer_bbox']==(0.0,10.0,0.0,10.0)
