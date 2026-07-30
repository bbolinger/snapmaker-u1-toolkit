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

# Resolution: env > u1_config 'orca_bin' > Linux deploy default. The config
# source persists across emitted child commands (see u1_config.get_orca_bin).
try:
    from u1_config import get_orca_bin as _get_orca_bin
    DEFAULT_ORCA = Path(_get_orca_bin())
except Exception:
    DEFAULT_ORCA = Path(os.environ.get('ORCA_SLICER_BIN', '/opt/data/tools/orcaslicer/squashfs-root/bin/orca-slicer'))
_VEC_RE = re.compile(r"\(?\s*([-+]?\d+(?:\.\d+)?)\s*[, ]\s*([-+]?\d+(?:\.\d+)?)\s*[, ]\s*([-+]?\d+(?:\.\d+)?)\s*\)?")
_COST_RE = re.compile(r"cost\s*[:=]\s*([-+]?\d+(?:\.\d+)?)", re.I)

def orca_env(orca_bin: Path = DEFAULT_ORCA) -> dict[str, str]:
    env=os.environ.copy()
    # LD_LIBRARY_PATH shimming only applies to the extracted-AppImage layout;
    # on other layouts (e.g. Windows portable, shallow paths) the lib dirs
    # simply don't exist and env is returned unchanged. parents[1] raises
    # IndexError for a binary sitting at filesystem root — treat as no-libs.
    try:
        root=orca_bin.resolve().parents[1]
    except (OSError, IndexError):
        return env
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

class TooManyPartsError(ValueError):
    """A 3MF build section exceeds the caller's part limit."""

# Cap for any single OPC model-XML entry parsed out of a 3MF/zip. Mirrors the
# kit extractor's per-part cap (u1_kit.MAX_PART_BYTES — not importable here
# without a cycle): zip entries are read wholly into RAM, so a tiny crafted
# archive declaring a multi-GB model entry would otherwise OOM the workflow
# before any human gate. zipfile enforces the declared size on read.
MAX_3MF_MODEL_BYTES = 200 * 1024 * 1024

def _read_model_entry(z: zipfile.ZipFile, name: str) -> bytes:
    info=z.getinfo(name)
    if info.file_size > MAX_3MF_MODEL_BYTES:
        raise ValueError(f'3MF model entry {name} is {info.file_size / 1e6:.0f}MB; the limit is {MAX_3MF_MODEL_BYTES / 1e6:.0f}MB')
    return z.read(name)

def _localname(tag: str) -> str:
    """Tag/attribute name with any ``{namespace}`` prefix stripped."""
    return tag.rsplit('}', 1)[-1]

def _attr_path(el: ET.Element) -> str | None:
    """The production-extension ``p:path`` attribute (cross-file object refs),
    matched namespace-agnostically — MakerWorld/Bambu 3MFs keep each object in
    its own ``3D/Objects/*.model`` and reference it this way."""
    for k, v in el.attrib.items():
        if _localname(k) == 'path':
            return v
    return None

def _transform_from_3mf(attr: str | None) -> tuple[np.ndarray, np.ndarray]:
    """3MF ``transform`` attribute → (R 3x3, t 3), row-vector convention.

    The spec's 12 numbers are the three rotation/scale rows m00..m22 followed
    by the translation m30 m31 m32; a vertex maps as ``v @ R + t``. An absent
    attribute is the identity. Malformed input raises ValueError so callers
    fall back to the fused-mesh path instead of silently misplacing a part.
    """
    if not attr:
        return np.eye(3, dtype=np.float64), np.zeros(3, dtype=np.float64)
    vals=[float(x) for x in attr.replace(',', ' ').split()]
    if len(vals) != 12:
        raise ValueError(f'3MF transform needs 12 numbers, got {len(vals)}')
    m=np.asarray(vals, dtype=np.float64)
    return m[:9].reshape(3, 3), m[9:]

def _parse_3mf_model(data: bytes) -> dict[str, object]:
    """One ``*.model`` XML → {'objects': {id: {...}}, 'build': [items]}."""
    root=ET.fromstring(data)
    objects: dict[str, dict]={}
    build: list[dict]=[]
    for el in root.iter():
        name=_localname(el.tag)
        if name == 'object':
            oid=el.attrib.get('id')
            if oid is None: continue
            entry: dict[str, object]={'name': el.attrib.get('name'), 'type': el.attrib.get('type', 'model'), 'mesh': None, 'components': []}
            for child in el:
                cname=_localname(child.tag)
                if cname == 'mesh':
                    entry['mesh']=child
                elif cname == 'components':
                    for comp in child:
                        if _localname(comp.tag) != 'component': continue
                        entry['components'].append({'objectid': comp.attrib.get('objectid'), 'path': _attr_path(comp), 'transform': comp.attrib.get('transform')})
            objects[oid]=entry
        elif name == 'item':
            build.append({'objectid': el.attrib.get('objectid'), 'path': _attr_path(el), 'transform': el.attrib.get('transform')})
    return {'objects': objects, 'build': build}

def _mesh_triangles(mesh_el: ET.Element) -> np.ndarray | None:
    """A ``<mesh>`` element → (n,3,3) float64 triangle array, None if empty."""
    verts=[]; faces=[]
    for el in mesh_el.iter():
        n=_localname(el.tag)
        if n == 'vertex':
            verts.append([float(el.attrib.get('x', '0')), float(el.attrib.get('y', '0')), float(el.attrib.get('z', '0'))])
        elif n == 'triangle':
            faces.append([int(el.attrib[k]) for k in ('v1', 'v2', 'v3')])
    if not verts or not faces:
        return None
    return np.asarray(verts, dtype=np.float64)[np.asarray(faces, dtype=np.int64)]

def _norm_zip_path(p: str) -> str:
    """p:path values are archive-absolute (``/3D/Objects/x.model``); zip entry
    names are not."""
    return p.replace('\\', '/').lstrip('/')

def _root_model_name(names: list[str]) -> str | None:
    models=[n for n in names if n.lower().endswith('.model')]
    if not models:
        return None
    for n in models:
        if _norm_zip_path(n).lower() == '3d/3dmodel.model':
            return n
    for n in models:
        if n.lower().endswith('3dmodel.model'):
            return n
    return models[0]

def _model_for(models: dict[str, dict], z: zipfile.ZipFile, name: str) -> dict:
    """Parsed model for an archive entry, cached per normalized path."""
    key=_norm_zip_path(name)
    if key in models:
        return models[key]
    try:
        data=_read_model_entry(z, name)
    except KeyError:
        match=next((n for n in z.namelist() if _norm_zip_path(n).lower() == key.lower()), None)
        if match is None:
            raise ValueError(f'3MF references missing model file {name}')
        data=_read_model_entry(z, match)
    models[key]=_parse_3mf_model(data)
    return models[key]

def _resolve_object_tris(models: dict[str, dict], z: zipfile.ZipFile, model_path: str, oid: str, R: np.ndarray, t: np.ndarray, depth: int, seen: frozenset) -> list[np.ndarray]:
    """World-space triangles for one object, recursing through its component
    tree. Transforms compose child-first: v' = v @ (C @ P) + (ct @ P + pt)."""
    if depth > 8:
        raise ValueError('3MF component nesting deeper than 8 levels')
    key=(model_path, oid)
    if key in seen:
        raise ValueError(f'3MF component cycle at object {oid}')
    obj=_model_for(models, z, model_path)['objects'].get(oid)
    if obj is None:
        raise ValueError(f'3MF object {oid} missing from {model_path}')
    out: list[np.ndarray]=[]
    if obj['mesh'] is not None:
        tris=_mesh_triangles(obj['mesh'])
        if tris is not None:
            world=tris @ R + t
            # A mirroring transform (negative determinant — 3MF allows it)
            # flips triangle winding, which turns the STL inside out. Restore
            # outward orientation by reversing each triangle's vertex order.
            if np.linalg.det(R) < 0:
                world=world[:, ::-1, :]
            out.append(world)
    for comp in obj['components']:
        cR, ct=_transform_from_3mf(comp['transform'])
        cpath=_norm_zip_path(comp['path']) if comp['path'] else model_path
        out.extend(_resolve_object_tris(models, z, cpath, comp['objectid'], cR @ R, ct @ R + t, depth + 1, seen | {key}))
    return out

def _safe_stem(s: str) -> str:
    s=re.sub(r'[^A-Za-z0-9_-]+', '_', s).strip('_')
    return s[:60] or 'part'

def extract_3mf_parts(path: Path, out_dir: Path, *, max_parts: int | None = None) -> list[Path]:
    """Split a 3MF into one world-space binary STL per build item.

    Returns [] when the file is not a 3MF or its build section has fewer than
    two items — the fused single-mesh path is already correct there and stays
    untouched. Per-item and per-component transforms, component trees, and
    production-extension p:path references into sibling ``*.model`` files are
    all resolved; two build items referencing the same object stay two parts
    (an authored plate with two copies is a kit of two). Raises
    TooManyPartsError past ``max_parts`` so ingest can reject cleanly instead
    of silently dropping parts.
    """
    path=Path(path); out_dir=Path(out_dir)
    if not zipfile.is_zipfile(path):
        return []
    with zipfile.ZipFile(path) as z:
        root_name=_root_model_name(z.namelist())
        if root_name is None:
            return []
        models: dict[str, dict]={}
        root=_model_for(models, z, root_name)
        items=[it for it in root['build'] if it.get('objectid')]
        if len(items) < 2:
            return []
        if max_parts is not None and len(items) > max_parts:
            raise TooManyPartsError(f'3MF build has {len(items)} items; the kit limit is {max_parts}')
        # Resolve EVERY item before writing anything: an exception on item N
        # must not leave items 1..N-1 as orphan STLs next to the fused
        # fallback's output.
        resolved: list[tuple[str, np.ndarray]]=[]
        used: set[str]=set()
        for i, it in enumerate(items):
            R, t=_transform_from_3mf(it.get('transform'))
            ipath=_norm_zip_path(it['path']) if it.get('path') else _norm_zip_path(root_name)
            tris_list=_resolve_object_tris(models, z, ipath, it['objectid'], R, t, 0, frozenset())
            if not tris_list:
                continue
            tris=np.concatenate(tris_list).astype(np.float32)
            obj=models[ipath]['objects'].get(it['objectid']) or {}
            stem=_safe_stem(str(obj.get('name') or f'{path.stem}_part{i + 1}'))
            base=stem; k=1
            while base in used:
                base=f'{stem}__{k}'; k += 1
            used.add(base)
            resolved.append((base, tris))
        if not resolved:
            return []
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path]=[]
        for base, tris in resolved:
            out=out_dir / f'{base}.stl'
            write_binary_stl(out, tris, name=base)
            written.append(out)
        return written

def count_3mf_build_items(path: Path) -> int:
    """Build items in an archive's root 3MF model, following one nesting level
    of ``.3mf``-inside-zip. 0 on anything unparseable. Cheap — XML only, no
    mesh resolution — so routing can call it on every upload."""
    try:
        path=Path(path)
        if not zipfile.is_zipfile(path):
            return 0
        with zipfile.ZipFile(path) as z:
            names=z.namelist()
            root_name=_root_model_name(names)
            if root_name is None:
                nested=[n for n in names if n.lower().endswith('.3mf')]
                if not nested or z.getinfo(nested[0]).file_size > MAX_3MF_MODEL_BYTES:
                    return 0
                with tempfile.TemporaryDirectory() as td:
                    tmp=Path(td) / Path(_norm_zip_path(nested[0])).name
                    tmp.write_bytes(z.read(nested[0]))
                    return count_3mf_build_items(tmp)
            root=_parse_3mf_model(_read_model_entry(z, root_name))
            return sum(1 for it in root['build'] if it.get('objectid'))
    except Exception:
        return 0

def _extract_from_zip(path: Path, out_dir: Path) -> Path:
    with zipfile.ZipFile(path) as z:
        names=z.namelist()
        stls=[n for n in names if n.lower().endswith('.stl')]
        if stls:
            name=stls[0]; out=out_dir / Path(name).name; out.write_bytes(z.read(name)); return out
        nested=[n for n in names if n.lower().endswith(('.3mf','.zip'))]
        if nested:
            info=z.getinfo(nested[0])
            if info.file_size > MAX_3MF_MODEL_BYTES:
                raise ValueError(f'nested archive {nested[0]} is {info.file_size / 1e6:.0f}MB; the limit is {MAX_3MF_MODEL_BYTES / 1e6:.0f}MB')
            tmp=out_dir / Path(nested[0]).name
            tmp.write_bytes(z.read(nested[0]))
            return extract_first_stl_from_3mf(tmp, out_dir)
        models=[n for n in names if n.lower().endswith('.model') or n.lower().endswith('3dmodel.model')]
        if models:
            tris=_triangles_from_3mf_model(_read_model_entry(z, models[0]))
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
