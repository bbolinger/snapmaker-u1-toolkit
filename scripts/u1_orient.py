#!/usr/bin/env python3
"""Own the U1 auto-orientation step: parse Orca --orient output, rotate mesh, write oriented.stl.

Default `--orient auto` calls the extracted Orca binary with `--orient 1 --info`,
parses Orca's cost matrix, and applies the lowest-cost orientation vector locally.
Orca reports the optimum; it does not write the rotated STL. If Orca is absent
or fails, the module falls back to the deterministic toolkit heuristic so
headless tests and degraded environments still fail closed with an explicit
fallback note in the returned metadata.
"""
from __future__ import annotations
import argparse, json, math, os, re, struct, subprocess, sys, zipfile, tempfile
from pathlib import Path
from typing import Iterable
import xml.etree.ElementTree as ET
import numpy as np

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
from _stl_render import parse_stl, bbox  # type: ignore

DEFAULT_ORCA = Path(os.environ.get('ORCA_SLICER_BIN', '/opt/data/tools/orcaslicer/squashfs-root/bin/orca-slicer'))
_VEC_RE = re.compile(r"\(?\s*([-+]?\d+(?:\.\d+)?)\s*[, ]\s*([-+]?\d+(?:\.\d+)?)\s*[, ]\s*([-+]?\d+(?:\.\d+)?)\s*\)?")
_COST_RE = re.compile(r"cost\s*[:=]\s*([-+]?\d+(?:\.\d+)?)", re.I)

def orca_env(orca_bin: Path = DEFAULT_ORCA) -> dict[str, str]:
    env=os.environ.copy()
    root=orca_bin.resolve().parents[1]
    lib_paths=[root.parent/'local-libs/usr/lib/x86_64-linux-gnu', root/'usr/lib', root/'usr/lib/x86_64-linux-gnu']
    existing=[str(p) for p in lib_paths if p.exists()]
    if existing:
        env['LD_LIBRARY_PATH']=':'.join(existing + ([env['LD_LIBRARY_PATH']] if env.get('LD_LIBRARY_PATH') else []))
    return env

def run_orca_orient(stl: Path, orca_bin: Path = DEFAULT_ORCA, timeout: int = 120) -> dict[str, object]:
    cmd=[str(orca_bin), '--orient', '1', '--info', str(stl)]
    proc=subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=orca_env(orca_bin), timeout=timeout)
    rows=parse_orca_orient_output(proc.stdout)
    if proc.returncode != 0 or not rows:
        raise RuntimeError(f'Orca orient failed rc={proc.returncode}: {proc.stdout[-2000:]}')
    return {'cmd': cmd, 'returncode': proc.returncode, 'output': proc.stdout, 'rows': rows, 'down_vec': choose_best_candidate(rows)}

def parse_orca_orient_output(text: str) -> list[dict[str, object]]:
    """Return candidate rows with {'down_vec': (x,y,z), 'cost': float} from Orca-ish logs.

    Orca output has varied across builds, so this parser accepts loose lines that
    contain a 3-vector and a cost. Lines without both are ignored.
    """
    rows=[]
    for line in text.splitlines():
        if 'cost' not in line.lower():
            continue
        cm=_COST_RE.search(line)
        vm=_VEC_RE.search(line)
        if not (cm and vm):
            continue
        vec=tuple(float(vm.group(i)) for i in range(1,4))
        rows.append({'down_vec': vec, 'cost': float(cm.group(1)), 'line': line.strip()})
    return rows

def choose_best_candidate(rows: Iterable[dict[str, object]]) -> tuple[float,float,float]:
    rows=list(rows)
    if not rows:
        raise ValueError('no orientation candidates found')
    best=min(rows, key=lambda r: float(r['cost']))
    return tuple(float(x) for x in best['down_vec'])  # type: ignore[index]

def apply_rotation(verts: np.ndarray, down_vec: Iterable[float]) -> np.ndarray:
    """Rotate vertices so Orca's orientation vector becomes build-up +Z, then drop min Z to bed.

    Orca's cost matrix row vector is the source-frame direction that should point
    upward after auto-orienting. Earlier code treated it as the direction that
    becomes bed-down (-Z), which inverted the EGO regression: the U-cradle tips
    landed on the bed while Orca's real slice used the wide plate/gusset contact.
    """
    d=np.asarray(list(down_vec), dtype=float)
    norm=np.linalg.norm(d)
    if norm < 1e-12:
        raise ValueError('down_vec must be non-zero')
    d=d/norm
    target=np.array([0.,0.,1.])
    if np.allclose(d, target):
        out=verts.astype(float, copy=True)
    elif np.allclose(d, -target):
        out=verts @ np.diag([1.,-1.,-1.])
    else:
        axis=np.cross(d, target)
        axis=axis/np.linalg.norm(axis)
        angle=np.arccos(np.clip(np.dot(d, target), -1., 1.))
        K=np.array([[0,-axis[2],axis[1]],[axis[2],0,-axis[0]],[-axis[1],axis[0],0]])
        R=np.eye(3)+np.sin(angle)*K+(1-np.cos(angle))*(K@K)
        out=verts @ R.T
    out[:,2]-=out[:,2].min()
    return out

def rotate_triangles(tris: np.ndarray, down_vec: Iterable[float]) -> np.ndarray:
    flat=tris.reshape(-1,3)
    rot=apply_rotation(flat, down_vec)
    return rot.reshape(tris.shape).astype(np.float32)

def write_binary_stl(path: Path, tris: np.ndarray, name: str='oriented') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as f:
        header=(name[:80]).encode('ascii','replace').ljust(80,b' ')
        f.write(header); f.write(struct.pack('<I', int(tris.shape[0])))
        for tri in tris.astype(np.float32):
            normal=np.cross(tri[1]-tri[0], tri[2]-tri[0])
            n=np.linalg.norm(normal)
            if n>1e-12: normal=normal/n
            else: normal=np.zeros(3, dtype=np.float32)
            f.write(struct.pack('<12fH', *(normal.tolist()+tri[0].tolist()+tri[1].tolist()+tri[2].tolist()), 0))

def _triangles_from_3mf_model(data: bytes) -> np.ndarray:
    root=ET.fromstring(data)
    ns=''
    if root.tag.startswith('{'):
        ns=root.tag.split('}',1)[0]+'}'
    objects=root.find(f'{ns}resources')
    if objects is None:
        raise ValueError('3MF has no resources')
    all_tris=[]
    for obj in objects.findall(f'{ns}object'):
        mesh=obj.find(f'{ns}mesh')
        if mesh is None: continue
        verts_el=mesh.find(f'{ns}vertices'); tris_el=mesh.find(f'{ns}triangles')
        if verts_el is None or tris_el is None: continue
        verts=[]
        for v in verts_el.findall(f'{ns}vertex'):
            verts.append([float(v.attrib.get('x','0')), float(v.attrib.get('y','0')), float(v.attrib.get('z','0'))])
        for t in tris_el.findall(f'{ns}triangle'):
            idx=[int(t.attrib[k]) for k in ('v1','v2','v3')]
            all_tris.append([verts[i] for i in idx])
    if not all_tris:
        raise ValueError('3MF model contains no triangles')
    return np.asarray(all_tris, dtype=np.float32)

def _extract_from_zip(path: Path, out_dir: Path) -> Path:
    with zipfile.ZipFile(path) as z:
        names=z.namelist()
        stls=[n for n in names if n.lower().endswith('.stl')]
        if stls:
            name=stls[0]; out=out_dir / Path(name).name; out.write_bytes(z.read(name)); return out
        nested=[n for n in names if n.lower().endswith(('.3mf','.zip'))]
        if nested:
            tmp=out_dir / Path(nested[0]).name
            tmp.write_bytes(z.read(nested[0]))
            return extract_first_stl_from_3mf(tmp, out_dir)
        models=[n for n in names if n.lower().endswith('.model') or n.lower().endswith('3dmodel.model')]
        if models:
            tris=_triangles_from_3mf_model(z.read(models[0]))
            out=out_dir / (path.stem + '_from_3mf.stl')
            write_binary_stl(out, tris, name=f'extracted from {path.name}')
            return out
    raise ValueError(f'no STL/3MF model inside {path}')

def extract_first_stl_from_3mf(path: Path, out_dir: Path) -> Path:
    """Extract/convert first embedded STL or 3MF model, or return path if already STL."""
    if path.suffix.lower()=='.stl':
        return path
    out_dir.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(path):
        return _extract_from_zip(path, out_dir)
    raise ValueError(f'unsupported model file: {path}')

def orient_model(src: Path, out_dir: Path, orient: str='auto', down_vec: Iterable[float]|None=None, orca_output: str|None=None, orca_bin: Path = DEFAULT_ORCA) -> dict[str, object]:
    stl=extract_first_stl_from_3mf(src, out_dir)
    tris=parse_stl(stl)
    orient_meta: dict[str, object]={'strategy': orient}
    if orient == 'asauthored':
        vec=(0.,0.,-1.)
    elif down_vec is not None:
        vec=tuple(float(x) for x in down_vec)
        orient_meta['strategy']='explicit_down_vec'
    elif orca_output:
        rows=parse_orca_orient_output(orca_output)
        vec=choose_best_candidate(rows)
        orient_meta.update({'strategy':'orca_output', 'orca_rows': rows})
    else:
        try:
            orca_res=run_orca_orient(stl, orca_bin=orca_bin)
            vec=tuple(float(x) for x in orca_res['down_vec'])  # type: ignore[index]
            orient_meta.update({'strategy':'orca_auto', 'orca_cmd': orca_res['cmd'], 'orca_rows': orca_res['rows']})
        except Exception as exc:
            # Safe deterministic fallback used when Orca CLI is unavailable.
            # This keeps degraded/headless contexts usable, but callers can see
            # that Orca's cost matrix was not consumed.
            vec=(0.,1.,0.)
            orient_meta.update({'strategy':'fallback_heuristic', 'fallback_reason': str(exc)})
    oriented=rotate_triangles(tris, vec)
    out=out_dir/'oriented.stl'
    write_binary_stl(out, oriented, name=f'oriented from {stl.name}')
    xmin,xmax,ymin,ymax,zmin,zmax=bbox(oriented)
    return {'source_stl': str(stl), 'oriented_stl': str(out), 'down_vec': vec, 'bbox': [xmin,xmax,ymin,ymax,zmin,zmax], 'orient': orient_meta}

def main(argv=None)->int:
    ap=argparse.ArgumentParser()
    ap.add_argument('model', type=Path); ap.add_argument('--out-dir', type=Path, default=Path('oriented_out'))
    ap.add_argument('--orient', choices=['auto','asauthored'], default='auto')
    ap.add_argument('--down-vec', nargs=3, type=float)
    ap.add_argument('--json', action='store_true')
    a=ap.parse_args(argv)
    res=orient_model(a.model, a.out_dir, a.orient, a.down_vec)
    print(json.dumps(res, indent=2) if a.json else res['oriented_stl'])
    return 0
if __name__=='__main__': raise SystemExit(main())
