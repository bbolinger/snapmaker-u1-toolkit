"""Tests for multi-object 3MF splitting (v3.1 — task: accept a real
MakerWorld/Printables multi-part 3MF as a kit).

Before this feature a multi-object .3mf was fused into ONE un-arrangeable
blob (`_triangles_from_3mf_model` concatenates every resource object and
ignores every transform). These tests pin the new behavior: one world-space
STL per build item, authored object names as part names, component trees and
production-extension p:path references resolved, and the fused path kept as
the fallback for everything that is not a genuine multi-object build.

Fixtures are synthesized OPC archives — no Orca, no network.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np
import pytest

import u1_kit
from u1_kit import KitIngestError
from u1_orient import (
    TooManyPartsError,
    count_3mf_build_items,
    extract_3mf_parts,
    parse_stl,
)

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    "</Types>"
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    "</Relationships>"
)


def _mesh_xml(s: float = 10.0) -> str:
    """A closed cube spanning [0, s]^3 as 3MF <mesh> XML."""
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
         (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    verts = "".join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in v)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    return f"<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh>"


def _model_xml(objects: str, build: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="millimeter" xmlns="{CORE_NS}" xmlns:p="{PROD_NS}">'
        f"<resources>{objects}</resources><build>{build}</build></model>"
    )


def _write_3mf(path: Path, model_xml: str,
               extra_entries: dict[str, str] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", model_xml)
        for name, data in (extra_entries or {}).items():
            z.writestr(name, data)
    return path


def _stl_bounds(stl: Path) -> tuple[np.ndarray, np.ndarray]:
    pts = parse_stl(stl).reshape(-1, 3)
    return pts.min(axis=0), pts.max(axis=0)


def _two_cube_model(name1: str = "Lid", name2: str = "Base") -> str:
    objects = (
        f'<object id="1" type="model" name="{name1}">{_mesh_xml()}</object>'
        f'<object id="2" type="model" name="{name2}">{_mesh_xml()}</object>'
    )
    build = (
        '<item objectid="1"/>'
        '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 100 50 0"/>'
    )
    return _model_xml(objects, build)


# ---------------------------------------------------------------------------
# The split itself
# ---------------------------------------------------------------------------

def test_two_objects_split_into_world_space_parts(tmp_path):
    src = _write_3mf(tmp_path / "kit.3mf", _two_cube_model())
    parts = extract_3mf_parts(src, tmp_path / "parts")
    assert [p.name for p in parts] == ["Lid.stl", "Base.stl"]

    lo1, hi1 = _stl_bounds(parts[0])
    assert np.allclose(lo1, [0, 0, 0]) and np.allclose(hi1, [10, 10, 10])
    lo2, hi2 = _stl_bounds(parts[1])
    assert np.allclose(lo2, [100, 50, 0]) and np.allclose(hi2, [110, 60, 10])


def test_row_vector_transform_convention(tmp_path):
    # 90-degree rotation about Z in the spec's row-vector layout: rows
    # (0,1,0)/(-1,0,0)/(0,0,1), translation (20,0,0). A [0,10]^3 cube maps
    # to x in [10,20], y in [0,10]. A transposed-matrix bug would put the
    # part at x in [20,30] instead — this is the projection-direction pin.
    objects = (
        f'<object id="1" name="A">{_mesh_xml()}</object>'
        f'<object id="2" name="B">{_mesh_xml()}</object>'
    )
    build = (
        '<item objectid="1"/>'
        '<item objectid="2" transform="0 1 0 -1 0 0 0 0 1 20 0 0"/>'
    )
    src = _write_3mf(tmp_path / "rot.3mf", _model_xml(objects, build))
    parts = extract_3mf_parts(src, tmp_path / "parts")
    lo, hi = _stl_bounds(parts[1])
    assert np.allclose(lo, [10, 0, 0]) and np.allclose(hi, [20, 10, 10])


def test_component_tree_composes_child_then_parent(tmp_path):
    # Object 2 references object 1 through a component with its own
    # translation; the build item adds another. World position must be the
    # sum (child transform applied first, then the item's).
    objects = (
        f'<object id="1" name="mesh-only">{_mesh_xml()}</object>'
        '<object id="2" name="Assembly"><components>'
        '<component objectid="1" transform="1 0 0 0 1 0 0 0 1 5 0 0"/>'
        "</components></object>"
        f'<object id="3" name="Solo">{_mesh_xml()}</object>'
    )
    build = (
        '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 7 0"/>'
        '<item objectid="3"/>'
    )
    src = _write_3mf(tmp_path / "comp.3mf", _model_xml(objects, build))
    parts = extract_3mf_parts(src, tmp_path / "parts")
    assert [p.name for p in parts] == ["Assembly.stl", "Solo.stl"]
    lo, hi = _stl_bounds(parts[0])
    assert np.allclose(lo, [5, 7, 0]) and np.allclose(hi, [15, 17, 10])


def test_duplicate_items_stay_separate_parts(tmp_path):
    # An authored plate holding two copies of one object is a kit of two.
    objects = f'<object id="1" name="Clip">{_mesh_xml()}</object>'
    build = (
        '<item objectid="1"/>'
        '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 30 0 0"/>'
    )
    src = _write_3mf(tmp_path / "dup.3mf", _model_xml(objects, build))
    parts = extract_3mf_parts(src, tmp_path / "parts")
    assert [p.name for p in parts] == ["Clip.stl", "Clip__1.stl"]
    assert not np.allclose(_stl_bounds(parts[0])[0], _stl_bounds(parts[1])[0])


def test_production_extension_p_path_objects(tmp_path):
    # MakerWorld/Bambu layout: the root model's build items point into
    # per-object model files via p:path.
    o1 = _model_xml(f'<object id="1" name="Left Bracket">{_mesh_xml()}</object>', "")
    o2 = _model_xml(f'<object id="1" name="Right Bracket">{_mesh_xml()}</object>', "")
    root = _model_xml(
        "",
        '<item objectid="1" p:path="/3D/Objects/object_1.model"/>'
        '<item objectid="1" p:path="/3D/Objects/object_2.model" '
        'transform="1 0 0 0 1 0 0 0 1 40 0 0"/>',
    )
    src = _write_3mf(tmp_path / "bambu.3mf", root, {
        "3D/Objects/object_1.model": o1,
        "3D/Objects/object_2.model": o2,
    })
    parts = extract_3mf_parts(src, tmp_path / "parts")
    assert [p.name for p in parts] == ["Left_Bracket.stl", "Right_Bracket.stl"]
    lo, _ = _stl_bounds(parts[1])
    assert np.allclose(lo, [40, 0, 0])


def test_object_names_sanitized_for_filesystem(tmp_path):
    src = _write_3mf(tmp_path / "n.3mf", _two_cube_model("Lid (v2)", "Base/Right"))
    parts = extract_3mf_parts(src, tmp_path / "parts")
    assert [p.name for p in parts] == ["Lid_v2.stl", "Base_Right.stl"]


# ---------------------------------------------------------------------------
# Fallbacks — everything that is NOT a multi-object build keeps old behavior
# ---------------------------------------------------------------------------

def test_single_build_item_keeps_fused_path(tmp_path):
    objects = f'<object id="1" name="One">{_mesh_xml()}</object>'
    src = _write_3mf(tmp_path / "one.3mf",
                     _model_xml(objects, '<item objectid="1"/>'))
    assert extract_3mf_parts(src, tmp_path / "p") == []
    stls = u1_kit.extract_all_stls(src, tmp_path / "parts")
    assert len(stls) == 1


def test_no_build_section_falls_back_fused(tmp_path):
    src = _write_3mf(tmp_path / "nb.3mf", _model_xml(
        f'<object id="1">{_mesh_xml()}</object>'
        f'<object id="2">{_mesh_xml()}</object>', ""))
    assert extract_3mf_parts(src, tmp_path / "p") == []
    stls = u1_kit.extract_all_stls(src, tmp_path / "parts")
    assert len(stls) == 1  # both meshes fused, as before


def test_malformed_transform_falls_back_fused(tmp_path):
    objects = (
        f'<object id="1">{_mesh_xml()}</object>'
        f'<object id="2">{_mesh_xml()}</object>'
    )
    build = '<item objectid="1"/><item objectid="2" transform="1 2 3"/>'
    src = _write_3mf(tmp_path / "bad.3mf", _model_xml(objects, build))
    with pytest.raises(ValueError):
        extract_3mf_parts(src, tmp_path / "p")
    # Ingest degrades to the fused blob rather than failing the kit.
    stls = u1_kit.extract_all_stls(src, tmp_path / "parts")
    assert len(stls) == 1


def test_component_cycle_is_caught_not_hung(tmp_path):
    objects = (
        '<object id="1"><components><component objectid="2"/></components></object>'
        '<object id="2"><components><component objectid="1"/></components></object>'
    )
    build = '<item objectid="1"/><item objectid="2"/>'
    src = _write_3mf(tmp_path / "cycle.3mf", _model_xml(objects, build))
    with pytest.raises(ValueError):
        extract_3mf_parts(src, tmp_path / "p")


def test_over_part_limit_rejects_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(u1_kit, "MAX_KIT_PARTS", 5)
    objects = f'<object id="1" name="C">{_mesh_xml()}</object>'
    build = "".join(
        f'<item objectid="1" transform="1 0 0 0 1 0 0 0 1 {i * 20} 0 0"/>'
        for i in range(6))
    src = _write_3mf(tmp_path / "big.3mf", _model_xml(objects, build))
    with pytest.raises(TooManyPartsError):
        extract_3mf_parts(src, tmp_path / "p", max_parts=5)
    with pytest.raises(KitIngestError):
        u1_kit.extract_all_stls(src, tmp_path / "parts")


# ---------------------------------------------------------------------------
# Kit-ingest integration + routing
# ---------------------------------------------------------------------------

def test_extract_all_stls_splits_bare_multi_3mf(tmp_path):
    src = _write_3mf(tmp_path / "kit.3mf", _two_cube_model())
    stls = u1_kit.extract_all_stls(src, tmp_path / "parts")
    assert sorted(p.name for p in stls) == ["Base.stl", "Lid.stl"]


def test_extract_all_stls_splits_zip_wrapped_3mf(tmp_path):
    inner = _write_3mf(tmp_path / "kit.3mf", _two_cube_model())
    wrapper = tmp_path / "download.zip"
    with zipfile.ZipFile(wrapper, "w") as z:
        z.writestr("README.txt", "printables download")
        z.write(inner, "files/kit.3mf")
    stls = u1_kit.extract_all_stls(wrapper, tmp_path / "parts")
    assert sorted(p.name for p in stls) == ["Base.stl", "Lid.stl"]


def test_zip_with_direct_stls_ignores_embedded_3mf(tmp_path):
    # Direct STL entries keep priority — the 3MF split only serves archives
    # with no loose STLs, exactly like the old single-extract deferral.
    from u1_orient import write_binary_stl
    cube = np.zeros((1, 3, 3), dtype=np.float32)
    cube[0] = [[0, 0, 0], [1, 0, 0], [0, 1, 0]]
    stl = tmp_path / "loose.stl"
    write_binary_stl(stl, cube)
    inner = _write_3mf(tmp_path / "kit.3mf", _two_cube_model())
    wrapper = tmp_path / "mixed.zip"
    with zipfile.ZipFile(wrapper, "w") as z:
        z.write(stl, "loose.stl")
        z.write(inner, "kit.3mf")
    stls = u1_kit.extract_all_stls(wrapper, tmp_path / "parts")
    assert [p.name for p in stls] == ["loose.stl"]


def test_build_kit_part_ids_from_object_names(tmp_path):
    src = _write_3mf(tmp_path / "kit.3mf", _two_cube_model())
    stls = u1_kit.extract_all_stls(src, tmp_path / "parts")
    kit = u1_kit.build_kit(stls)
    assert kit["part_count"] == 2 and kit["multi"] is True
    assert [p["part_id"] for p in kit["parts"]] == ["01_Lid", "02_Base"]


def test_count_and_routing_helpers(tmp_path):
    multi = _write_3mf(tmp_path / "multi.3mf", _two_cube_model())
    single = _write_3mf(
        tmp_path / "single.3mf",
        _model_xml(f'<object id="1">{_mesh_xml()}</object>',
                   '<item objectid="1"/>'))
    assert count_3mf_build_items(multi) == 2
    assert count_3mf_build_items(single) == 1
    assert count_3mf_build_items(tmp_path / "missing.3mf") == 0
    assert u1_kit.is_multi_part_archive(multi) is True
    assert u1_kit.is_multi_part_archive(single) is False

    wrapper = tmp_path / "w.zip"
    with zipfile.ZipFile(wrapper, "w") as z:
        z.write(multi, "kit.3mf")
    assert count_3mf_build_items(wrapper) == 2
    assert u1_kit.is_multi_part_archive(wrapper) is True
