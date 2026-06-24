from __future__ import annotations
import numpy as np
from pathlib import Path
from u1_orient import apply_rotation, parse_orca_orient_output, choose_best_candidate, rotate_triangles, write_binary_stl, orient_model
from _stl_render import parse_stl, bbox

def test_parse_orca_cost_matrix_loose_text():
    rows=parse_orca_orient_output('candidate down=(0,1,0) cost=1795\nother down=(0,0,-1) cost=7366')
    assert len(rows)==2
    assert choose_best_candidate(rows)==(0.0,1.0,0.0)

def test_parse_real_orca_cost_matrix_picks_lowest_cost_row_2():
    text='''
overhang, bottom, bothull, contour, A_laf, A_prj, unprintability
orientation:-0.0000 -0.0000  1.0000, cost: 7366.2,   404.9,   302.3,    80.5,  734.9, 0.0, 22.8
orientation:-0.0000  1.0000 -0.0000, cost: 1795.1, 16682.0, 11177.9,   516.6, 1640.2, 0.0,  0.1
orientation:-1.0000 -0.0000  0.0000, cost:11296.9,   443.4,   264.9,    84.2, 3765.7, 0.0, 32.6
orientation: 1.0000 -0.0000 -0.0000, cost:11294.1,   443.4,   264.9,    84.2, 3765.7, 0.0, 32.6
orientation:-0.0000 -1.0000 -0.0000, cost:14011.2,   933.3,  1335.6,   122.2, 1568.3, 0.0, 18.1
'''
    rows=parse_orca_orient_output(text)
    assert len(rows)==5
    assert choose_best_candidate(rows)==(-0.0,1.0,-0.0)

def test_rotation_identity_and_drop():
    verts=np.array([[0,0,-2],[1,0,-1]], dtype=float)
    out=apply_rotation(verts, (0,0,1))
    assert np.isclose(out[:,2].min(),0)
    assert np.allclose(out[:,0], verts[:,0])

def test_rotation_antiparallel():
    out=apply_rotation(np.array([[0,2,3.]], dtype=float), (0,0,-1))
    assert np.isclose(out[0,2], 0)

def test_rotation_x_to_up():
    v=np.array([[1.,0,0],[2,0,0]])
    out=apply_rotation(v, (1,0,0))
    assert np.allclose(out[:,2], [0,1], atol=1e-6)

def test_rotation_y_to_up():
    v=np.array([[0.,1,0],[0,2,0]])
    out=apply_rotation(v, (0,1,0))
    assert np.allclose(out[:,2], [0,1], atol=1e-6)

def test_write_and_read_oriented_stl(tmp_path):
    tris=np.array([[[0,0,0],[1,0,0],[0,1,0]]], dtype=np.float32)
    out=tmp_path/'a.stl'; write_binary_stl(out, tris)
    parsed=parse_stl(out)
    assert parsed.shape == (1,3,3)

def test_orient_model_falls_back_when_orca_unavailable(tmp_path):
    src=tmp_path/'src.stl'; write_binary_stl(src, np.array([[[0,0,0],[1,0,0],[0,1,0]]], dtype=np.float32))
    res=orient_model(src, tmp_path/'out', orca_bin=tmp_path/'missing-orca')
    assert Path(res['oriented_stl']).exists()
    assert res['orient']['strategy']=='fallback_heuristic'
    assert tuple(res['down_vec'])==(0.0,1.0,0.0)
    assert abs(res['bbox'][4]) < 1e-6
