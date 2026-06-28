"""Arrange + multi-plate slice — v2.1.0 Phase D.

Slices a *kit* (one or more selected STLs) in a single Orca invocation using
``--arrange 1`` so Orca lays the parts out on the bed and — when they overflow
— splits them across ``plate_1.gcode … plate_N.gcode`` itself. We collect every
plate, hash each, and rewrite the toolhead per plate. We do NOT pack or split:
that is Orca's job (spike-verified, see docs/v2.1.0-multipart-kits-plan.md §2).

This sits alongside ``u1_slice_workflow.real_orca_slice`` (the single-STL path)
and reuses its profile-resolution helpers verbatim, so a kit slices through the
exact same machine/process/filament chain. The single-STL path is untouched.

Gate-detection note (§0): this is a pure slice primitive. It takes a concrete
list of parts + decisions and returns plate facts. It makes no decisions and
talks to no printer. The workflow state machine calls it; the model never does.

Spike-verified facts this depends on:
  - positional STLs + ``--arrange 1`` slice headless (rc=0); ``--arrange 0``
    fails (rc=206) so we always pass ``--arrange 1`` for a kit.
  - overflow auto-splits into ``plate_N.gcode`` in ``--outputdir``.
  - ``--orient 1`` and ``--allow-rotations`` compose in the same call.
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
) -> dict[str, Any]:
    """Slice a kit into plates with Orca auto-arrange.

    Returns ``{plate_count, plates: [{plate_idx, gcode_path, gcode_hash,
    metadata}], cmd}``. Each plate's toolhead is rewritten T0→T<tool> (the kit
    is single-material in v2.1; per-part material is deferred). Thumbnails are
    intentionally skipped here — a single-part thumbnail would misrepresent a
    multi-part plate; revisit with a plate render later (fail-soft, like the
    single-STL path treats thumbnails).

    ``auto_orient`` adds ``--orient 1``. Verified on the real binary
    (2026-06-28) that ``--orient 1`` + multiple objects + ``--arrange 1`` runs
    headless without crashing and yields valid plates; the *quality* of
    per-object orientation is Orca's own heuristic and is not independently
    verified here. The operator reviews the readiness card + Stage-1 photo
    before any start, so a poor orient is caught by the human, not silently
    printed. Kits default to as-authored (``auto_orient=False``) since
    Printables kits are usually pre-oriented.
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

    # Clear any stale plate gcode so the post-slice glob is unambiguous. The
    # old "diff before/after, fall back to all if nothing fresh" pattern could
    # leak a plate from a prior slice into this result when a re-slice produced
    # fewer plates (review finding, 2026-06-28). Deleting first makes the count
    # exact. Inputs are *.stl, so this never touches an input.
    for stale in out_dir.glob("plate_*.gcode"):
        stale.unlink()

    cmd = build_arrange_cmd(
        stl_paths, out_dir,
        machine=machine, process=process, filament=filament, orca_bin=orca_bin,
        auto_orient=auto_orient, allow_rotations=allow_rotations,
    )

    proc = (runner or _default_runner)(cmd, orca_bin)
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-4000:]
        raise RuntimeError(f"Orca arrange-slice failed rc={proc.returncode}: {tail}")

    plates_found = sorted(out_dir.glob("plate_*.gcode"), key=_plate_index)
    if not plates_found:
        raise RuntimeError("Orca arrange-slice produced no plate gcode")

    tool_idx = _tool_to_index(tool)
    plates: list[dict[str, Any]] = []
    for g in plates_found:
        if g.stat().st_size == 0:
            raise RuntimeError(f"empty plate gcode: {g.name}")
        rewrite_gcode_for_tool(g, tool_idx)
        plates.append({
            "plate_idx": _plate_index(g),
            "gcode_path": str(g),
            "gcode_hash": u1_request.compute_model_hash(g),
            "metadata": parse_gcode_metadata(g).get("metadata", {}),
        })

    return {"plate_count": len(plates), "plates": plates, "cmd": cmd}
