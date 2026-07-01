"""Arrange + multi-plate slice — v2.1.0 Phase D (with bed-overflow guard).

Slices a *kit* (one or more selected STLs) by invoking Orca's ``--arrange 1``
flag. Until 2026-06-30 we trusted Orca to auto-split overflow into
``plate_1.gcode … plate_N.gcode``. Live print AUDIT 2026-06-30 (the
"angles teaching aid" 8-part kit) proved this assumption WRONG: Orca packed
all 8 parts onto a virtual plate 269×270mm wide (bed is 220×220), produced a
single overflowing ``plate_1.gcode``, and returned rc=0 with no warning. The
printer then executed extrusion moves out to X=270 / Y=270 — parts printing
off the bed, into the endstops, with no fault surfaced upstream.

This module now does two things on top of the original Orca call:

1. **Bed-overflow guard** — after Orca returns, parse the gcode's XY extrusion
   extent. If it exceeds the bed dimensions (with margin), refuse the plate
   and trigger auto-split.

2. **Manual multi-plate split** — when overflow is detected, partition the
   STL list across N plates using first-fit-decreasing bin packing by
   footprint area, then re-invoke Orca once per partition. Each plate is
   independently validated against the bed.

The original single-plate path is preserved as ``_run_orca_one_plate``; the
top-level ``arrange_slice`` orchestrates the all-parts → validate → maybe-
split flow.

Sits alongside ``u1_slice_workflow.real_orca_slice`` (single-STL path) and
reuses its profile-resolution helpers verbatim — kit slices go through the
exact same machine/process/filament chain. Single-STL path is untouched.

Gate-detection note (§0): this is a pure slice primitive. It takes a concrete
list of parts + decisions and returns plate facts. It makes no decisions and
talks to no printer. The workflow state machine calls it; the model never does.

Spike-verified facts this depends on:
  - positional STLs + ``--arrange 1`` slice headless (rc=0); ``--arrange 0``
    fails (rc=206) so we always pass ``--arrange 1`` for a kit.
  - ``--orient 1`` and ``--allow-rotations`` compose in the same call.
  - Orca DOES NOT auto-split overflow into multi-plate output despite docs
    claiming so (2026-06-30 audit). We split manually now.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from u1_orient import DEFAULT_ORCA, orca_env
from u1_slice_workflow import (
    machine_profile_for_orca,
    profile_path,
    filament_path,
    _materialize_flat_filament,
    rewrite_gcode_for_tool,
    _tool_to_index,
)
from u1_upload_gcode import parse_gcode_metadata
import u1_request

_PLATE_RE = re.compile(r"plate_(\d+)\.gcode$", re.IGNORECASE)

# Snapmaker U1 bed in mm + the margin Orca needs for the brim/skirt.
# Source: the OFFICIAL Snapmaker U1 OrcaSlicer machine profile (verified
# 2026-06-30 against printable_area ['0.5x1', '270.5x1', '270.5x271',
# '0.5x271'] in resources/profiles/Snapmaker/machine/Snapmaker U1 (0.4
# nozzle).json). The U1 build volume is 270×270×270mm. The toolkit's
# u1_kit.DEFAULT_BED_MM had this wrong as 220x220 — corrected here +
# in u1_kit.py during the 2026-06-30 audit that uncovered the false-alarm
# bed-overflow cancellation.
_BED_MM: tuple[float, float] = (270.0, 270.0)
_BED_MARGIN_MM: float = 5.0

# Target packing density per plate for the manual partitioner. Orca achieves
# ~60–70% on rectangular parts at default --allow-rotations; 50% leaves room
# for brim/skirt + the non-rectangular silhouettes Orca can't pack tightly.
# Empirically tuned on the angles-teaching-aid 8-part kit (2026-06-30) —
# at 55% the resulting plate brim drifted 0.3mm over the bed edge.
_TARGET_PLATE_FILL = 0.50

# Brim/skirt slop the validator tolerates beyond the strict bed envelope.
# Orca's default brim is 5mm — typical drift is much smaller. 3mm matches
# the printer's physical print-area clipping behavior on the U1 firmware.
_BRIM_SLOP_MM = 3.0


class BedOverflowError(RuntimeError):
    """Raised when a sliced plate exceeds the bed dimensions even after
    manual partitioning (e.g. a single part is too big for the bed)."""


def _gcode_extrusion_xy_extent(path: Path) -> dict[str, float] | None:
    """Parse a sliced gcode and return the XY extent of all EXTRUSION moves.

    Travel moves are ignored — Orca can park the head outside the bed for
    purging / wiping and that's OK. Only extrusion (`G[01] X.. Y.. E+`) tells
    us where actual material is deposited.

    Returns ``{'xmin', 'xmax', 'ymin', 'ymax', 'width', 'depth',
    'extrude_moves'}`` or ``None`` when no extrusion moves are found.
    """
    move_x = re.compile(r"\bX(-?\d+\.?\d*)")
    move_y = re.compile(r"\bY(-?\d+\.?\d*)")
    move_e = re.compile(r"\bE(-?\d+\.?\d*)")
    xmin = ymin = float("inf")
    xmax = ymax = float("-inf")
    last_x = last_y = None
    extrudes = 0
    with path.open() as fh:
        for line in fh:
            if not line.startswith("G"):
                continue
            mx = move_x.search(line)
            my = move_y.search(line)
            if mx:
                last_x = float(mx.group(1))
            if my:
                last_y = float(my.group(1))
            if last_x is None or last_y is None:
                continue
            me = move_e.search(line)
            if me and float(me.group(1)) > 0:
                if last_x < xmin: xmin = last_x
                if last_x > xmax: xmax = last_x
                if last_y < ymin: ymin = last_y
                if last_y > ymax: ymax = last_y
                extrudes += 1
    if extrudes == 0:
        return None
    return {"xmin": xmin, "xmax": xmax, "ymin": ymin, "ymax": ymax,
            "width": xmax - xmin, "depth": ymax - ymin,
            "extrude_moves": extrudes}


def _extent_within_bed(extent: dict[str, float] | None,
                       bed_mm: tuple[float, float] = _BED_MM,
                       margin_mm: float = _BED_MARGIN_MM,
                       brim_slop_mm: float = _BRIM_SLOP_MM) -> tuple[bool, str | None]:
    """Return ``(within_bed, reason)``. ``reason`` is None when within.

    ``brim_slop_mm`` allows the extrusion envelope to extend slightly beyond
    the strict bed edge to accommodate brim/skirt drift. Set to 0 for a
    strict check (used to detect the original 269×270mm overflow bug).
    """
    if extent is None:
        return False, "no extrusion moves found in gcode"
    max_x = bed_mm[0] + brim_slop_mm
    max_y = bed_mm[1] + brim_slop_mm
    min_x = -brim_slop_mm
    min_y = -brim_slop_mm
    if (extent["xmin"] < min_x or extent["xmax"] > max_x
            or extent["ymin"] < min_y or extent["ymax"] > max_y):
        return False, (
            f"extrusion extent {extent['xmin']:.1f}..{extent['xmax']:.1f}x"
            f"{extent['ymin']:.1f}..{extent['ymax']:.1f}mm exceeds bed "
            f"{bed_mm[0]:.0f}x{bed_mm[1]:.0f}mm (slop ±{brim_slop_mm:.0f}mm)"
        )
    return True, None


def _stl_footprint_area(path: Path) -> float:
    """Approximate footprint area (mm²) from STL XY bbox. Used by the
    partitioner. Cheap: parses the STL once and takes max(X)-min(X) * same Y."""
    try:
        # u1_kit.summarize_part already does this work; reuse if cheap.
        from _stl_render import parse_stl as _parse  # type: ignore
    except Exception:
        # Fallback to u1_kit.summarize_part which does the same and adds extra fields.
        import u1_kit as _kit
        info = _kit.summarize_part(path)
        fp = info.get("footprint_mm") or [0.0, 0.0]
        return max(0.0, float(fp[0]) * float(fp[1]))
    try:
        tris = _parse(path)
        xs = tris[:, :, 0].flatten()
        ys = tris[:, :, 1].flatten()
        return max(0.0, (float(xs.max() - xs.min())) * (float(ys.max() - ys.min())))
    except Exception:
        return 0.0


def _partition_parts(stl_paths: list[Path],
                     bed_mm: tuple[float, float] = _BED_MM,
                     margin_mm: float = _BED_MARGIN_MM,
                     fill: float = _TARGET_PLATE_FILL) -> list[list[Path]]:
    """First-fit-decreasing bin packing by footprint area.

    Sorts parts largest-first, then greedily fills plates up to
    ``(bed_x - margin) * (bed_y - margin) * fill``. Returns a list of
    plate part-lists. Parts whose own area exceeds the cap go on their
    own plate (the per-plate validator will catch a truly oversized part
    by raising BedOverflowError).
    """
    usable_x = bed_mm[0] - margin_mm
    usable_y = bed_mm[1] - margin_mm
    cap = usable_x * usable_y * fill
    areas = {p: _stl_footprint_area(p) for p in stl_paths}
    sorted_paths = sorted(stl_paths, key=lambda p: -areas[p])
    plates: list[list] = []  # each: [used_area, [paths]]
    for p in sorted_paths:
        area = areas[p]
        placed = False
        for plate in plates:
            if plate[0] + area <= cap:
                plate[0] += area
                plate[1].append(p)
                placed = True
                break
        if not placed:
            plates.append([area, [p]])
    return [pl[1] for pl in plates]


def _plate_index(path: Path) -> int:
    """Numeric plate index from a ``plate_N.gcode`` filename (0 if unparsable)."""
    m = _PLATE_RE.search(Path(path).name)
    return int(m.group(1)) if m else 0


def _default_runner(cmd: list[str], orca_bin: Path) -> subprocess.CompletedProcess:
    """Run Orca with the toolkit's standard headless env. Injectable for tests."""
    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=orca_env(orca_bin),
        timeout=900,
    )


def build_arrange_cmd(
    stl_paths: list[Path] | list[str],
    out_dir: Path,
    *,
    machine: Path,
    process: Path,
    filament: Path,
    orca_bin: Path,
    auto_orient: bool,
    allow_rotations: bool,
) -> list[str]:
    """Construct the Orca CLI command for an arranged multi-part slice.

    ``--arrange 1`` is mandatory (``--arrange 0`` fails with multiple objects).
    ``--orient 1`` is added only when the operator chose auto-orient — footprint
    changes with orientation, so this must be decided before arrange. Parts are
    passed as positional args (NOT ``--load-assemble-list``, which segfaults
    headless).
    """
    cmd = [
        str(orca_bin),
        "--load-settings", f"{machine};{process}",
        "--load-filaments", str(filament),
        "--outputdir", str(out_dir),
        "--arrange", "1",
    ]
    if auto_orient:
        cmd += ["--orient", "1"]
    if allow_rotations:
        cmd += ["--allow-rotations"]
    cmd += ["--slice", "0"]
    cmd += [str(p) for p in stl_paths]
    return cmd


def _run_orca_arrange_export_stls(
    stl_paths: list[Path],
    work_dir: Path,
    *,
    machine: Path,
    process: Path,
    filament: Path,
    orca_bin: Path,
    auto_orient: bool,
    allow_rotations: bool,
    timeout: int = 300,
) -> dict[str, Any]:
    """Best-effort: run Orca once with ``--arrange 1 --export-stl --export-3mf``
    to produce BOTH per-part arranged STLs AND the arrangement 3MF sidecar
    (the 3MF carries exact per-item transforms Orca used
    for placement, so the render helper can reproduce the arrangement
    perfectly regardless of profile/infill choice).

    Returns ``{'stl_paths': [Path, ...], 'arrange_3mf': Path | None}``.
    On failure returns empty stls + None arrange_3mf (NEVER raises — the
    render path is fully downgradeable).

    Uses LC_ALL=C, writable cwd, XDG_RUNTIME_DIR — the same env fixes needed
    to work around the SIGABRT crash in headless Orca --export-3mf/--export-stl
    (2026-06-30 audit).
    """
    empty: dict[str, Any] = {"stl_paths": [], "arrange_3mf": None}
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        env = orca_env(orca_bin)
        env["LC_ALL"] = "C"
        env["XDG_RUNTIME_DIR"] = str(work_dir)
        arrange_3mf_name = "arrange.3mf"  # relative — 3MF export needs writable cwd + relative filename
        cmd = [
            str(orca_bin),
            "--load-settings", f"{machine};{process}",
            "--load-filaments", str(filament),
            "--outputdir", str(work_dir),
            "--arrange", "1",
        ]
        if auto_orient:
            cmd += ["--orient", "1"]
        if allow_rotations:
            cmd += ["--allow-rotations"]
        cmd += ["--export-stl", "--export-3mf", arrange_3mf_name]
        cmd += [str(p) for p in stl_paths]
        proc = subprocess.run(
            cmd, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, cwd=str(work_dir), timeout=timeout,
        )
        if proc.returncode != 0:
            # If combined stl+3mf fails, retry with just --export-stl so we
            # at least get the STLs (render can fall through to alt paths).
            cmd_stl_only = [c for c in cmd
                            if c not in ("--export-3mf", arrange_3mf_name)]
            proc = subprocess.run(
                cmd_stl_only, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, cwd=str(work_dir), timeout=timeout,
            )
            if proc.returncode != 0:
                return empty
        # Orca --export-stl writes to a `stl/` SUBDIRECTORY under --outputdir
        # (verified 2026-07-01 in Hermes container: files land at
        # <outputdir>/stl/obj_<N>_<original_stem>.stl). Also glob top-level
        # as a safety net in case a future Orca build changes the layout.
        stls = sorted(work_dir.glob("stl/obj_*.stl"))
        if not stls:
            stls = sorted(work_dir.glob("obj_*.stl"))
        # 3MF may be relative to cwd (work_dir) since we passed a relative name.
        arrange_3mf: Path | None = None
        cand = work_dir / arrange_3mf_name
        if cand.exists() and cand.stat().st_size > 0:
            arrange_3mf = cand
        return {"stl_paths": stls, "arrange_3mf": arrange_3mf}
    except Exception:
        return empty


def _run_orca_one_plate(
    stl_paths: list[Path],
    out_dir: Path,
    *,
    machine: Path,
    process: Path,
    filament: Path,
    orca_bin: Path,
    auto_orient: bool,
    allow_rotations: bool,
    runner: Callable[[list[str], Path], subprocess.CompletedProcess] | None,
) -> tuple[Path, list[str]]:
    """Invoke Orca once for a single plate's worth of parts. Returns
    (gcode_path, cmd). Caller is responsible for clearing the out_dir
    beforehand if a clean glob is needed."""
    cmd = build_arrange_cmd(
        stl_paths, out_dir,
        machine=machine, process=process, filament=filament, orca_bin=orca_bin,
        auto_orient=auto_orient, allow_rotations=allow_rotations,
    )
    proc = (runner or _default_runner)(cmd, orca_bin)
    plates_found = sorted(out_dir.glob("plate_*.gcode"), key=_plate_index)
    if proc.returncode != 0:
        # Orca returns rc=154 ("return -102") when the arranged plate
        # overflows the bed — but it STILL writes plate_1.gcode with the
        # overflowing arrangement. Verified 2026-07-01 smoke test with the
        # 8-part angles kit: plain --slice 0 rc=154 + 17MB gcode on disk.
        # The 2026-06-30 audit's manual partitioner is designed for exactly
        # this case, but only ran when Orca returned rc=0 with an overflow.
        # When a plate WAS written despite the non-zero rc, treat as
        # overflow warning and let the caller (arrange_slice) validate
        # extent + partition manually.
        if not plates_found:
            tail = (proc.stdout or "")[-4000:]
            raise RuntimeError(f"Orca arrange-slice failed rc={proc.returncode}: {tail}")
    if not plates_found:
        raise RuntimeError("Orca arrange-slice produced no plate gcode")
    if len(plates_found) > 1:
        # Orca's --arrange occasionally writes multiple plate_*.gcode files
        # internally when its own threshold trips. We don't trust the internal
        # split (audit 2026-06-30 caught the opposite case), so when we see
        # this we keep the first one and raise — the manual partitioner will
        # re-run with a smaller part list.
        raise RuntimeError(
            f"Orca produced {len(plates_found)} plates internally; "
            "this code expects exactly one plate per invocation. Caller "
            "should partition the part list."
        )
    return plates_found[0], cmd


def arrange_slice(
    stl_paths: list[Path] | list[str],
    out_dir: Path,
    *,
    tool: str,
    material: str,
    profile: str,
    nozzle: str = "0.4",
    auto_orient: bool = False,
    allow_rotations: bool = True,
    orca_bin: Path = DEFAULT_ORCA,
    process_path_override: Path | None = None,
    runner: Callable[[list[str], Path], subprocess.CompletedProcess] | None = None,
    bed_mm: tuple[float, float] = _BED_MM,
    bed_margin_mm: float = _BED_MARGIN_MM,
) -> dict[str, Any]:
    """Slice a kit into plates with Orca arrange + manual overflow split.

    Returns ``{plate_count, plates: [{plate_idx, gcode_path, gcode_hash,
    metadata, bed_extent}], cmd, partition, was_split}``. Each plate's
    toolhead is rewritten T0→T<tool> (single-material; per-part material
    deferred). Plate-level thumbnails are still skipped (deferred to the
    composite-render Phase 2.6 follow-up).

    Two-phase strategy (audit 2026-06-30):

    1. **First pass: all parts on one plate.** Invoke Orca with the full
       STL list. Inspect the resulting gcode's extrusion XY extent.
       - If within bed: done, single-plate kit.
       - If overflow: fall to phase 2.

    2. **Manual partition + per-plate slice.** Bin-pack STLs by footprint
       area (first-fit-decreasing, target 55 percent bed fill), invoke
       Orca once per partition, validate each plate against the bed,
       collect results. ``was_split`` is True; ``partition`` lists the
       part-IDs assigned to each plate.

    Raises ``BedOverflowError`` when even a single-part plate exceeds the
    bed (oversized part; u1_kit.build_kit's ``oversized_part_ids`` should
    have caught this earlier — surface it as a clear error here).

    ``auto_orient`` adds ``--orient 1``. Kits default to as-authored
    (``auto_orient=False``) — Printables kits are usually pre-oriented.
    """
    stl_paths = [Path(p) for p in stl_paths]
    if not stl_paths:
        raise ValueError("arrange_slice requires at least one STL")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    machine = machine_profile_for_orca(orca_bin)
    process = process_path_override if process_path_override else profile_path(profile)
    filament = _materialize_flat_filament(
        filament_path(material, nozzle=nozzle), out_dir, orca_bin=orca_bin
    )
    tool_idx = _tool_to_index(tool)

    def _clean_plate_gcodes(in_dir: Path) -> None:
        """Remove any stale plate_*.gcode in a slice dir so post-slice glob
        is unambiguous. Inputs are *.stl, never touched."""
        for stale in in_dir.glob("plate_*.gcode"):
            stale.unlink()

    def _finalize_plate(src_gcode: Path, dest_path: Path,
                        plate_idx: int) -> dict[str, Any]:
        """Validate extent + rewrite tool + hash + collect metadata."""
        if src_gcode.stat().st_size == 0:
            raise RuntimeError(f"empty plate gcode: {src_gcode.name}")
        extent = _gcode_extrusion_xy_extent(src_gcode)
        ok, reason = _extent_within_bed(extent, bed_mm, bed_margin_mm)
        if not ok:
            raise BedOverflowError(
                f"plate {plate_idx} extent overflow: {reason} (gcode at {src_gcode})"
            )
        if src_gcode != dest_path:
            src_gcode.replace(dest_path)
        rewrite_gcode_for_tool(dest_path, tool_idx)
        return {
            "plate_idx": plate_idx,
            "gcode_path": str(dest_path),
            "gcode_hash": u1_request.compute_model_hash(dest_path),
            "metadata": parse_gcode_metadata(dest_path).get("metadata", {}),
            "bed_extent": extent,
        }

    # ── PHASE 1: try all parts on one plate ──
    _clean_plate_gcodes(out_dir)
    try:
        single_path, single_cmd = _run_orca_one_plate(
            stl_paths, out_dir,
            machine=machine, process=process, filament=filament,
            orca_bin=orca_bin, auto_orient=auto_orient,
            allow_rotations=allow_rotations, runner=runner,
        )
    except RuntimeError as exc:
        # Orca produced multiple plates internally — we don't trust that
        # behavior post-audit; fall to manual split.
        if "produced" in str(exc).lower() and "plates internally" in str(exc):
            single_path = None
            single_cmd = []
        else:
            raise

    if single_path is not None:
        extent = _gcode_extrusion_xy_extent(single_path)
        ok, _reason = _extent_within_bed(extent, bed_mm, bed_margin_mm)
        if ok:
            # All parts fit on one plate — clean exit.
            plate_dest = out_dir / "plate_1.gcode"
            finalized = _finalize_plate(single_path, plate_dest, plate_idx=1)
            # Best-effort arranged-STL + 3MF export for the plate render.
            # 3MF carries exact per-item transforms so
            # the render can reproduce Orca's arrangement perfectly with
            # source STL geometry — profile-independent, always clean.
            arranged = _run_orca_arrange_export_stls(
                stl_paths, out_dir / "plate_1_arranged_stls",
                machine=machine, process=process, filament=filament,
                orca_bin=orca_bin, auto_orient=auto_orient,
                allow_rotations=allow_rotations,
            )
            finalized["arranged_stls"] = [str(p) for p in arranged["stl_paths"]]
            finalized["arrange_3mf"] = (str(arranged["arrange_3mf"])
                                        if arranged["arrange_3mf"] else None)
            finalized["source_stls"] = [str(p) for p in stl_paths]
            return {
                "plate_count": 1,
                "plates": [finalized],
                "cmd": single_cmd,
                "partition": [[str(p) for p in stl_paths]],
                "was_split": False,
            }

    # ── PHASE 2: manual partition + per-plate slice ──
    _clean_plate_gcodes(out_dir)
    partitions = _partition_parts(stl_paths, bed_mm, bed_margin_mm)
    partition_summary = [[Path(p).name for p in pl] for pl in partitions]

    plates: list[dict[str, Any]] = []
    cmds: list[list[str]] = []
    for i, part_subset in enumerate(partitions, start=1):
        plate_workdir = out_dir / f"plate_{i}_work"
        plate_workdir.mkdir(exist_ok=True)
        _clean_plate_gcodes(plate_workdir)
        plate_gcode, plate_cmd = _run_orca_one_plate(
            part_subset, plate_workdir,
            machine=machine, process=process, filament=filament,
            orca_bin=orca_bin, auto_orient=auto_orient,
            allow_rotations=allow_rotations, runner=runner,
        )
        cmds.append(plate_cmd)
        # Move from work dir up to the top-level out_dir with plate_N.gcode name.
        dest = out_dir / f"plate_{i}.gcode"
        if dest.exists():
            dest.unlink()
        finalized = _finalize_plate(plate_gcode, dest, plate_idx=i)
        finalized["partition_parts"] = [Path(p).name for p in part_subset]
        arranged = _run_orca_arrange_export_stls(
            part_subset, out_dir / f"plate_{i}_arranged_stls",
            machine=machine, process=process, filament=filament,
            orca_bin=orca_bin, auto_orient=auto_orient,
            allow_rotations=allow_rotations,
        )
        finalized["arranged_stls"] = [str(p) for p in arranged["stl_paths"]]
        finalized["arrange_3mf"] = (str(arranged["arrange_3mf"])
                                    if arranged["arrange_3mf"] else None)
        finalized["source_stls"] = [str(p) for p in part_subset]
        plates.append(finalized)

    return {
        "plate_count": len(plates),
        "plates": plates,
        "cmd": cmds[0] if cmds else [],
        "all_cmds": cmds,
        "partition": partition_summary,
        "was_split": True,
    }
