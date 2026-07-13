"""Multi-part kit ingest — v2.1.0 Phase A.

A *kit* is a set of parts printed together. The common Printables shape is a
zip of individual STL files dropped onto one bed and auto-arranged. This module
turns such an archive (or an explicit list of model files) into a normalized
list of parts — each with a stable id, content hash, and footprint — so the
workflow can offer selection and feed all selected parts to Orca's ``--arrange``.

A single STL is just a kit of one part, so existing single-file behavior is
preserved by that framing.

Design note (gate-detection principle — see docs/v2.1.0-multipart-kits-plan.md
§0): this module is pure ingest + measurement. It makes NO slicing decisions and
talks to NO network/printer. The workflow state machine consumes its output and
owns the gates. Keeping ingest dumb is what lets the model stay dumb.

Spike-verified facts this serves (§2 of the plan):
  - Orca arranges multiple positional STLs with ``--arrange 1`` headless.
  - Orca does NOT auto-split overflow (2026-06-30 audit); u1_arrange owns
    partitioning (first-fit-decreasing) — we still do not pack or split here.
  - ``--allow-rotations`` lets arrange rotate a part in-plane, so a part that
    only fits rotated still fits. ``part_fits_bed`` accounts for that.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

# u1_orient sets up the tools/ path and re-exports the geometry helpers.
from u1_orient import (
    parse_stl,
    bbox,
    extract_first_stl_from_3mf,
)
import u1_request

# Snapmaker U1 bed in mm. Source: the OFFICIAL Snapmaker U1 OrcaSlicer
# machine profile (verified 2026-06-30 against printable_area ['0.5x1',
# '270.5x1', '270.5x271', '0.5x271'] in
# /opt/data/tools/orcaslicer/.../profiles/Snapmaker/machine/Snapmaker U1
# (0.4 nozzle).json). The U1 build volume is 270×270×270mm.
#
# This was wrong as (220, 220) until the 2026-06-30 audit. The wrong value
# caused the bed-overflow guard to false-alarm + abort a valid kit print
# (the actual gcode extent was 269×270mm — within the real 270 bed).
DEFAULT_BED_MM: tuple[float, float] = (270.0, 270.0)
# Clearance Orca needs around each part when arranging. Only used for fit hints.
ARRANGE_MARGIN_MM: float = 5.0

# Ingest limits. Extraction reads each entry wholly into RAM and summarize_
# part loads the STL into numpy — without caps a crafted kit zip (thousands
# of entries, or one multi-GB STL of zeros) OOMs the workflow before any
# human gate. Generous for real kits: the largest Printables kits are a few
# dozen parts and well under 100MB per STL.
MAX_KIT_PARTS = 100
MAX_PART_BYTES = 200 * 1024 * 1024        # per extracted STL
MAX_KIT_TOTAL_BYTES = 600 * 1024 * 1024   # sum of extracted STLs


class KitIngestError(ValueError):
    """Clean, operator-facing ingest refusal (bad archive / over limits).
    The workflow catches this and emits kit_rejected instead of a traceback."""


def _check_archive_limits(stl_infos: list) -> None:
    """Refuse archives that exceed the ingest caps BEFORE extracting."""
    if len(stl_infos) > MAX_KIT_PARTS:
        raise KitIngestError(
            f"kit has {len(stl_infos)} STL entries; the limit is "
            f"{MAX_KIT_PARTS}. Split the archive.")
    total = 0
    for info in stl_infos:
        if info.file_size > MAX_PART_BYTES:
            raise KitIngestError(
                f"entry {info.filename!r} is {info.file_size / 1e6:.0f}MB "
                f"uncompressed; the per-part limit is "
                f"{MAX_PART_BYTES / 1e6:.0f}MB.")
        total += info.file_size
    if total > MAX_KIT_TOTAL_BYTES:
        raise KitIngestError(
            f"kit is {total / 1e6:.0f}MB uncompressed across "
            f"{len(stl_infos)} STLs; the limit is "
            f"{MAX_KIT_TOTAL_BYTES / 1e6:.0f}MB.")


def _sanitize(stem: str) -> str:
    """Filesystem/grammar-safe token from a filename stem (for part ids)."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    return s or "part"


def extract_all_stls(archive: Path, out_dir: Path) -> list[Path]:
    """Return every STL inside ``archive`` (archive order, deterministic).

    - A zip containing ``.stl`` entries → one extracted file per entry.
      Identical basenames from different folders are de-duplicated by suffix so
      no extraction clobbers another.
    - A zip with no direct STLs (nested 3MF/.model only) → falls back to the
      single-extract path (one part); a multi-object 3MF is sliced directly by
      Orca from embedded positions, so splitting it here is unnecessary in v2.1.
    - A bare ``.stl`` / ``.3mf`` (not a zip) → a kit of one part.
    """
    archive = Path(archive)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not zipfile.is_zipfile(archive):
        return [extract_first_stl_from_3mf(archive, out_dir)]

    with zipfile.ZipFile(archive) as z:
        stl_infos = [i for i in z.infolist()
                     if i.filename.lower().endswith(".stl")]
        if not stl_infos:
            # Defer to single-extract: handles nested .3mf / .model archives.
            return [extract_first_stl_from_3mf(archive, out_dir)]
        _check_archive_limits(stl_infos)

        extracted: list[Path] = []
        used: set[str] = set()
        for info in stl_infos:
            n = info.filename
            # Basename-only defeats POSIX zip-slip; the backslash replace
            # covers Windows-style entry names, where Path(...).name would
            # return the whole "..\\..\\evil.stl" string.
            base = Path(n.replace("\\", "/")).name
            # Dedup against every name USED so far — not a per-name counter.
            # The old `__N` scheme could collide with a genuine `part__1.stl`
            # archive entry and silently overwrite it (one part lost, one
            # duplicated) because write_bytes clobbers unconditionally.
            if base in used:
                p = Path(base)
                k = 1
                while f"{p.stem}__{k}{p.suffix}" in used:
                    k += 1
                base = f"{p.stem}__{k}{p.suffix}"
            used.add(base)
            out = out_dir / base
            out.write_bytes(z.read(n))
            extracted.append(out)
        return extracted


def count_archive_stls(archive: Path) -> int:
    """Number of ``.stl`` entries in a zip (0 if not a zip or none present)."""
    archive = Path(archive)
    if not zipfile.is_zipfile(archive):
        return 0
    with zipfile.ZipFile(archive) as z:
        return sum(1 for n in z.namelist() if n.lower().endswith(".stl"))


def is_multi_part_archive(archive: Path) -> bool:
    """True if the archive holds more than one STL — i.e. a kit that should be
    routed to the kit workflow rather than the single-STL workflow."""
    return count_archive_stls(archive) > 1


def resolve_upload_path(path: Path) -> Path:
    """Map a possibly-mangled upload path back to the real file on disk.

    The driving agent sometimes retypes an uploaded doc's name and mangles the
    human-readable suffix (e.g. a '+' becomes '_'), but the ``doc_<hash>``
    prefix is unique and stable. If ``path`` is missing, glob the parent for
    that prefix and use the sole match; fall back to ``path`` when the basename
    isn't a doc upload, the glob is ambiguous, or any FS access fails.
    """
    path = Path(path)
    if path.exists():
        return path
    m = re.match(r"^(doc_[0-9a-f]{6,})_", path.name)
    if not m:
        return path
    try:
        matches = [p for p in path.parent.glob(f"{m.group(1)}_*") if p.is_file()]
    except Exception:
        return path
    return matches[0] if len(matches) == 1 else path


def part_fits_bed(
    footprint_mm: tuple[float, float] | list[float],
    bed_mm: tuple[float, float] = DEFAULT_BED_MM,
    margin_mm: float = ARRANGE_MARGIN_MM,
) -> bool:
    """Whether a single part can sit on the bed, allowing a 90° rotation.

    A part bigger than the usable bed in both orientations can never be
    arranged — Orca would reject the whole job. We surface that as a clean
    per-part message instead of a raw slice failure.
    """
    fx, fy = float(footprint_mm[0]), float(footprint_mm[1])
    usable_x = bed_mm[0] - margin_mm
    usable_y = bed_mm[1] - margin_mm
    fits_as_is = fx <= usable_x and fy <= usable_y
    fits_rotated = fy <= usable_x and fx <= usable_y
    return fits_as_is or fits_rotated


def summarize_part(stl: Path) -> dict[str, Any]:
    """Measure one part: filename, content hash, bbox, footprint, height.

    Footprint is measured **as-authored** (the STL's current orientation). If
    the operator later chooses auto-orient, Orca may reorient the part and its
    real footprint will differ — so ``fits_bed`` derived from this is a
    pre-orientation hint, not a guarantee. The actual fit is decided by Orca's
    arrange at slice time (a part that truly can't fit fails the slice, which
    the workflow surfaces).
    """
    stl = Path(stl)
    tris = parse_stl(stl)
    xmin, xmax, ymin, ymax, zmin, zmax = bbox(tris)
    return {
        "filename": stl.name,
        "path": str(stl),
        "model_hash": u1_request.compute_model_hash(stl),
        "bbox_mm": [xmin, xmax, ymin, ymax, zmin, zmax],
        "footprint_mm": [xmax - xmin, ymax - ymin],
        "height_mm": zmax - zmin,
    }


def build_kit(
    stl_paths: list[Path] | list[str],
    *,
    bed_mm: tuple[float, float] = DEFAULT_BED_MM,
) -> dict[str, Any]:
    """Build the ``kit`` record from extracted STL paths.

    Each part gets a stable ``part_id`` (``NN_<sanitized-stem>``, ordered by
    input position) and ``selected: True`` by default. ``oversized_part_ids``
    flags parts that can't fit the bed even rotated.
    """
    parts: list[dict[str, Any]] = []
    for i, p in enumerate(stl_paths):
        info = summarize_part(Path(p))
        stem = Path(info["filename"]).stem
        info["part_id"] = f"{i + 1:02d}_{_sanitize(stem)}"
        info["selected"] = True
        info["fits_bed"] = part_fits_bed(info["footprint_mm"], bed_mm)
        parts.append(info)

    oversized = [p["part_id"] for p in parts if not p["fits_bed"]]
    return {
        "parts": parts,
        "part_count": len(parts),
        "multi": len(parts) > 1,
        "bed_mm": [bed_mm[0], bed_mm[1]],
        "oversized_part_ids": oversized,
    }
