from __future__ import annotations
import argparse, json, numpy as np, pytest
from pathlib import Path
from u1_orient import write_binary_stl, DEFAULT_ORCA
from u1_slice_workflow import main, run_workflow
from render_slice_review import first_layer_bbox
from _stl_render import parse_stl

def _stl(tmp_path):
    p=tmp_path/'m.stl'
    verts=np.array([
        [0,0,0],[20,0,0],[20,20,0],[0,20,0],
        [0,0,5],[20,0,5],[20,20,5],[0,20,5],
    ], dtype=np.float32)
    faces=[(0,1,2),(0,2,3),(4,6,5),(4,7,6),(0,4,5),(0,5,1),(1,5,6),(1,6,2),(2,6,7),(2,7,3),(3,7,4),(3,4,0)]
    write_binary_stl(p, np.array([[verts[a],verts[b],verts[c]] for a,b,c in faces], dtype=np.float32))
    return p

def test_headless_yes_upload_only_has_no_prompts(tmp_path, capsys):
    src=_stl(tmp_path); out=tmp_path/'out'
    rc=main([str(src),'--yes','--upload-only','--no-live-material','--tool','T1','--material','PETG','--profile','020_strength','--out-dir',str(out)])
    captured=capsys.readouterr().out
    assert rc==0 and 'uploaded' in captured and 'dry_run' in captured
    assert (out/'initial_render.png').exists() and (out/'preview.png').exists()

def test_json_events_surface_questions_and_summary(tmp_path, capsys):
    src=_stl(tmp_path); main([str(src),'--json-events','--yes','--no-live-material','--out-dir',str(tmp_path/'o')])
    events=[json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith('{')]
    keys=[e.get('key') for e in events if e.get('stage')=='need_input']
    assert keys[:4]==['orient','tool','preset','supports']
    assert any(e.get('stage')=='summary' for e in events)


def test_workflow_output_oriented_stl_matches_orca_slice_first_layer_for_ego(tmp_path):
    """Real-Orca regression for the inverted orientation bug.

    The EGO trimmer's correct Orca auto-orient has a wide first-layer footprint.
    Treating Orca's row vector as bed-down puts U-cradle tips on the bed and
    produces a tiny/narrow contact instead. This test exercises the full workflow
    with extracted Orca and compares the rendered/oriented STL with the actual
    sliced G-code footprint.
    """
    ego=Path('/opt/data/cache/documents/doc_9d706d1d9b73_EGO String Trimmer holder v4.3mf.zip')
    if not ego.exists():
        pytest.skip('EGO regression source not present in this environment')
    if not DEFAULT_ORCA.exists():
        pytest.skip('extracted Orca binary not present in this environment')
    args=argparse.Namespace(
        model=str(ego), json_events=False, yes=True, orient='auto', down_vec=None,
        tool='T1', material='PETG', profile='020_strength', class_hint='ego trimmer holder',
        upload_only=True, live_upload=False, no_live_material=True,
        out_dir=tmp_path/'ego_real_orca', cancel=False,
    )
    res=run_workflow(args)
    gcode_bbox=first_layer_bbox(Path(res['gcode']))
    assert gcode_bbox is not None
    gx0,gx1,gy0,gy1=gcode_bbox
    gwidth=max(gx1-gx0, gy1-gy0)
    gdepth=min(gx1-gx0, gy1-gy0)
    assert gwidth > 100
    assert gdepth > 70

    tris=parse_stl(Path(res['oriented_stl']))
    contact=tris.reshape(-1,3)
    contact=contact[contact[:,2] <= 0.6]
    assert contact.size > 0
    sx0,sy0=contact[:,0].min(), contact[:,1].min()
    sx1,sy1=contact[:,0].max(), contact[:,1].max()
    swidth=max(sx1-sx0, sy1-sy0)
    sdepth=min(sx1-sx0, sy1-sy0)
    # Orca includes brim in the first-layer G-code footprint. The oriented STL
    # contact patch should therefore be smaller than G-code by a roughly even
    # brim margin on both axes, not the tiny U-cradle-tip footprint that caught
    # the inverted-rotation bug.
    assert abs((gwidth-swidth) - (gdepth-sdepth)) < 8
    assert 20 < (gwidth-swidth) < 45
    assert 20 < (gdepth-sdepth) < 45
    assert Path(res['preview']).exists()
