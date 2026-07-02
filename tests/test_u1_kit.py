"""Tests for scripts/u1_kit.py — multi-part kit ingest (v2.1.0 Phase A).

No Orca needed: pure ingest + measurement. Cube STL fixtures are generated with
the same binary-STL writer the toolkit uses, so parse_stl reads them faithfully.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np

import u1_kit
from u1_orient import write_binary_stl


def _cube_tris(s: float) -> np.ndarray:
    """12-triangle axis-aligned cube spanning [0, s]^3."""
    v = np.array(
        [[0, 0, 0], [s, 0, 0], [s, s, 0], [0, s, 0],
         [0, 0, s], [s, 0, s], [s, s, s], [0, s, s]],
        dtype=np.float32,
    )
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=np.float32)


def _box_tris(x: float, y: float, z: float) -> np.ndarray:
    """Axis-aligned box [0,x]x[0,y]x[0,z] (for footprint/fit tests)."""
    v = np.array(
        [[0, 0, 0], [x, 0, 0], [x, y, 0], [0, y, 0],
         [0, 0, z], [x, 0, z], [x, y, z], [0, y, z]],
        dtype=np.float32,
    )
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=np.float32)


def _write_cube(path: Path, s: float) -> Path:
    write_binary_stl(path, _cube_tris(s), name=path.stem)
    return path


def _make_zip(zip_path: Path, members: dict[str, Path]) -> Path:
    with zipfile.ZipFile(zip_path, "w") as z:
        for arcname, src in members.items():
            z.write(src, arcname)
    return zip_path


# --------------------------------------------------------------------------- #
# extract_all_stls
# --------------------------------------------------------------------------- #

def test_extract_all_stls_from_zip_returns_every_part(tmp_path):
    a = _write_cube(tmp_path / "a.stl", 20)
    b = _write_cube(tmp_path / "b.stl", 25)
    c = _write_cube(tmp_path / "c.stl", 30)
    zp = _make_zip(tmp_path / "kit.zip", {"a.stl": a, "b.stl": b, "c.stl": c})

    out = tmp_path / "out"
    got = u1_kit.extract_all_stls(zp, out)
    assert len(got) == 3
    assert all(p.exists() and p.stat().st_size > 0 for p in got)
    assert {p.name for p in got} == {"a.stl", "b.stl", "c.stl"}


def test_extract_all_stls_dedups_identical_basenames(tmp_path):
    a = _write_cube(tmp_path / "part.stl", 20)
    zp = tmp_path / "dup.zip"
    # Same basename in two folders — must not clobber each other.
    with zipfile.ZipFile(zp, "w") as z:
        z.write(a, "left/part.stl")
        z.write(a, "right/part.stl")

    out = tmp_path / "out"
    got = u1_kit.extract_all_stls(zp, out)
    assert len(got) == 2
    names = sorted(p.name for p in got)
    assert names[0] == "part.stl"
    assert names[1].startswith("part__")  # de-duped
    assert len({p.resolve() for p in got}) == 2  # distinct files on disk


def test_extract_all_stls_bare_stl_is_kit_of_one(tmp_path):
    a = _write_cube(tmp_path / "solo.stl", 20)
    got = u1_kit.extract_all_stls(a, tmp_path / "out")
    assert len(got) == 1
    assert got[0].suffix.lower() == ".stl"


def test_extract_all_stls_zip_without_stls_falls_back_single(tmp_path, monkeypatch):
    # A zip with no .stl entries should defer to the single-extract path.
    zp = tmp_path / "noStl.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("readme.txt", "no models here")

    sentinel = tmp_path / "converted.stl"
    _write_cube(sentinel, 10)
    called = {}

    def fake_single(archive, out_dir):
        called["hit"] = True
        return sentinel

    monkeypatch.setattr(u1_kit, "extract_first_stl_from_3mf", fake_single)
    got = u1_kit.extract_all_stls(zp, tmp_path / "out")
    assert called.get("hit") is True
    assert got == [sentinel]


# --------------------------------------------------------------------------- #
# summarize_part / footprint
# --------------------------------------------------------------------------- #

def test_summarize_part_measures_footprint_and_hash(tmp_path):
    p = _write_cube(tmp_path / "cube30.stl", 30)
    info = u1_kit.summarize_part(p)
    assert info["filename"] == "cube30.stl"
    assert info["model_hash"] and isinstance(info["model_hash"], str)
    fx, fy = info["footprint_mm"]
    assert abs(fx - 30) < 1e-3 and abs(fy - 30) < 1e-3
    assert abs(info["height_mm"] - 30) < 1e-3


# --------------------------------------------------------------------------- #
# part_fits_bed
# --------------------------------------------------------------------------- #

def test_part_fits_bed_small_part_fits():
    assert u1_kit.part_fits_bed((50, 50)) is True


def test_part_fits_bed_oversized_both_axes_does_not_fit():
    assert u1_kit.part_fits_bed((300, 300)) is False


def test_part_fits_bed_long_thin_at_default_bed_limits():
    # U1 bed is 270 x 270 (DEFAULT_BED_MM), usable 265 x 265 after the 5mm
    # arrange margin. On a square bed both orientations share the limit.
    assert u1_kit.part_fits_bed((10, 264)) is True
    assert u1_kit.part_fits_bed((264, 10)) is True
    assert u1_kit.part_fits_bed((266, 10)) is False  # exceeds usable on the long axis


def test_part_fits_bed_rotation_branch_on_rectangular_bed():
    # A rectangular bed actually exercises the 90°-rotation branch: 240 x 90
    # fails as-is on a 100 x 300 bed (usable 95 x 295) but fits rotated.
    bed = (100.0, 300.0)
    assert u1_kit.part_fits_bed((240, 90), bed_mm=bed) is True   # only via rotation
    assert u1_kit.part_fits_bed((240, 96), bed_mm=bed) is False  # too wide either way


# --------------------------------------------------------------------------- #
# build_kit
# --------------------------------------------------------------------------- #

def test_build_kit_multi_part(tmp_path):
    paths = [
        _write_cube(tmp_path / "alpha.stl", 20),
        _write_cube(tmp_path / "beta.stl", 25),
    ]
    kit = u1_kit.build_kit(paths)
    assert kit["part_count"] == 2
    assert kit["multi"] is True
    ids = [p["part_id"] for p in kit["parts"]]
    assert ids == ["01_alpha", "02_beta"]
    assert all(p["selected"] is True for p in kit["parts"])
    assert all(p["model_hash"] for p in kit["parts"])
    assert kit["oversized_part_ids"] == []


def test_build_kit_single_part_is_not_multi(tmp_path):
    kit = u1_kit.build_kit([_write_cube(tmp_path / "solo.stl", 20)])
    assert kit["part_count"] == 1
    assert kit["multi"] is False


def test_build_kit_flags_oversized_part(tmp_path):
    paths = [
        _write_cube(tmp_path / "ok.stl", 20),
        # 300x300 footprint cannot fit the 270 bed even rotated.
        (lambda p: (write_binary_stl(p, _box_tris(300, 300, 10), name="big"), p)[1])(
            tmp_path / "toobig.stl"
        ),
    ]
    kit = u1_kit.build_kit(paths)
    assert kit["oversized_part_ids"] == ["02_toobig"]
    assert kit["parts"][0]["fits_bed"] is True
    assert kit["parts"][1]["fits_bed"] is False


def test_build_kit_part_ids_unique_and_ordered(tmp_path):
    paths = [_write_cube(tmp_path / f"m{i}.stl", 10 + i) for i in range(3)]
    kit = u1_kit.build_kit(paths)
    ids = [p["part_id"] for p in kit["parts"]]
    assert ids == ["01_m0", "02_m1", "03_m2"]
    assert len(set(ids)) == 3


# --------------------------------------------------------------------------- #
# kit detection (routing)
# --------------------------------------------------------------------------- #

def test_is_multi_part_archive_true_for_multi_stl_zip(tmp_path):
    a = _write_cube(tmp_path / "a.stl", 20)
    b = _write_cube(tmp_path / "b.stl", 25)
    zp = _make_zip(tmp_path / "kit.zip", {"a.stl": a, "b.stl": b})
    assert u1_kit.is_multi_part_archive(zp) is True
    assert u1_kit.count_archive_stls(zp) == 2


def test_is_multi_part_archive_false_for_single_stl_zip(tmp_path):
    a = _write_cube(tmp_path / "a.stl", 20)
    zp = _make_zip(tmp_path / "solo.zip", {"a.stl": a})
    assert u1_kit.is_multi_part_archive(zp) is False


def test_is_multi_part_archive_false_for_bare_stl(tmp_path):
    a = _write_cube(tmp_path / "a.stl", 20)
    assert u1_kit.is_multi_part_archive(a) is False


# --------------------------------------------------------------------------- #
# Ingest hardening (v2.1.0-rc2 round 2)
# --------------------------------------------------------------------------- #

def test_extract_dedup_does_not_clobber_real_suffixed_entry(tmp_path):
    # Archive holds a/part.stl, a GENUINE part__1.stl, and b/part.stl. The
    # old per-name counter renamed b/part.stl to part__1.stl and overwrote
    # the real one — one part destroyed, one duplicated, no error.
    a = _write_cube(tmp_path / "A.stl", 10)
    real = _write_cube(tmp_path / "B.stl", 20)
    b = _write_cube(tmp_path / "C.stl", 30)
    zp = tmp_path / "kit.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.write(a, "a/part.stl")
        z.write(real, "part__1.stl")
        z.write(b, "b/part.stl")
    out = u1_kit.extract_all_stls(zp, tmp_path / "out")
    assert len(out) == 3
    assert len(set(out)) == 3, "no duplicate paths"
    contents = sorted(p.read_bytes() for p in out)
    expected = sorted(p.read_bytes() for p in (a, real, b))
    assert contents == expected, "every distinct part's bytes must survive"


def test_extract_sanitizes_backslash_entry_names(tmp_path):
    # Windows-style entry names: Path('..\\..\\evil.stl').name is the whole
    # string on POSIX — must not escape (or weirdly name) the out dir.
    a = _write_cube(tmp_path / "a.stl", 10)
    zp = tmp_path / "kit.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.write(a, "..\\..\\evil.stl")
        z.write(a, "ok.stl")
    out_dir = tmp_path / "out"
    out = u1_kit.extract_all_stls(zp, out_dir)
    for p in out:
        assert p.parent == out_dir
        assert "\\" not in p.name and ".." not in p.name.split(".stl")[0].replace("evil", "")
    assert any(p.name == "evil.stl" for p in out)


def test_extract_refuses_too_many_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(u1_kit, "MAX_KIT_PARTS", 3)
    a = _write_cube(tmp_path / "a.stl", 10)
    zp = tmp_path / "kit.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for i in range(4):
            z.write(a, f"p{i}.stl")
    import pytest
    with pytest.raises(u1_kit.KitIngestError, match="limit is 3"):
        u1_kit.extract_all_stls(zp, tmp_path / "out")


def test_extract_refuses_oversized_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(u1_kit, "MAX_PART_BYTES", 100)
    a = _write_cube(tmp_path / "a.stl", 10)  # binary STL cube > 100 bytes
    zp = tmp_path / "kit.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.write(a, "big.stl")
        z.write(a, "big2.stl")
    import pytest
    with pytest.raises(u1_kit.KitIngestError, match="per-part limit"):
        u1_kit.extract_all_stls(zp, tmp_path / "out")


def test_extract_refuses_oversized_total(tmp_path, monkeypatch):
    a = _write_cube(tmp_path / "a.stl", 10)
    size = a.stat().st_size
    monkeypatch.setattr(u1_kit, "MAX_PART_BYTES", size + 10)
    monkeypatch.setattr(u1_kit, "MAX_KIT_TOTAL_BYTES", size + 10)  # 2 entries exceed
    zp = tmp_path / "kit.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.write(a, "a.stl")
        z.write(a, "b.stl")
    import pytest
    with pytest.raises(u1_kit.KitIngestError, match="limit is"):
        u1_kit.extract_all_stls(zp, tmp_path / "out")
