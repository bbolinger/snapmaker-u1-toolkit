"""Tests for scripts/u1_arrange.py — arrange + multi-plate slice (Phase D).

Unit tests are hermetic: the Orca runner is injected and profile/rewrite/
metadata helpers are monkeypatched, so no Orca binary or real profiles are
needed. A separate live test (run by hand in hermes-agent-stack) exercises the
real binary; see docs/v2.1.0-multipart-kits-plan.md §2.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import u1_arrange


# --------------------------------------------------------------------------- #
# build_arrange_cmd
# --------------------------------------------------------------------------- #

def test_build_arrange_cmd_has_arrange_and_positional_stls():
    cmd = u1_arrange.build_arrange_cmd(
        [Path("a.stl"), Path("b.stl")], Path("/out"),
        machine=Path("m.json"), process=Path("p.json"), filament=Path("f.json"),
        orca_bin=Path("/orca"), auto_orient=False, allow_rotations=True,
    )
    assert "--arrange" in cmd and cmd[cmd.index("--arrange") + 1] == "1"
    assert "--slice" in cmd and cmd[cmd.index("--slice") + 1] == "0"
    assert cmd[-2:] == ["a.stl", "b.stl"]  # positional STLs last
    assert "--load-assemble-list" not in cmd  # the segfault path — never used
    assert "--allow-rotations" in cmd
    assert "--orient" not in cmd  # auto_orient False


def test_build_arrange_cmd_auto_orient_and_no_rotation():
    cmd = u1_arrange.build_arrange_cmd(
        [Path("a.stl")], Path("/out"),
        machine=Path("m.json"), process=Path("p.json"), filament=Path("f.json"),
        orca_bin=Path("/orca"), auto_orient=True, allow_rotations=False,
    )
    assert "--orient" in cmd and cmd[cmd.index("--orient") + 1] == "1"
    assert "--allow-rotations" not in cmd


def test_plate_index_parsing():
    assert u1_arrange._plate_index(Path("plate_1.gcode")) == 1
    assert u1_arrange._plate_index(Path("/x/plate_12.gcode")) == 12
    assert u1_arrange._plate_index(Path("weird.gcode")) == 0


# --------------------------------------------------------------------------- #
# arrange_slice (hermetic)
# --------------------------------------------------------------------------- #

@pytest.fixture
def hermetic(monkeypatch):
    """Stub the profile/rewrite/metadata helpers so arrange_slice runs without
    Orca or real profiles. Returns a dict recording tool rewrites."""
    monkeypatch.setattr(u1_arrange, "machine_profile_for_orca", lambda *a, **k: Path("machine.json"))
    monkeypatch.setattr(u1_arrange, "profile_path", lambda p: Path("process.json"))
    monkeypatch.setattr(u1_arrange, "filament_path", lambda m, nozzle="0.4": Path("fil.json"))
    monkeypatch.setattr(u1_arrange, "_materialize_flat_filament", lambda fr, od, orca_bin=None: Path("fil_flat.json"))
    rewrites = {"calls": []}
    monkeypatch.setattr(u1_arrange, "rewrite_gcode_for_tool",
                        lambda g, idx: rewrites["calls"].append((Path(g).name, idx)) or 1)
    monkeypatch.setattr(u1_arrange, "parse_gcode_metadata", lambda g: {"metadata": {"filament_type": "PLA"}})
    return rewrites


def _runner_writing(n_plates: int, rc: int = 0):
    """Build a runner that writes n plate gcode files into the --outputdir."""
    def _run(cmd, orca_bin):
        out_dir = Path(cmd[cmd.index("--outputdir") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, n_plates + 1):
            (out_dir / f"plate_{i}.gcode").write_text(f"; fake plate {i}\nT0\nG1 X0\n")
        return subprocess.CompletedProcess(cmd, rc, stdout="ok", stderr="")
    return _run


def test_arrange_slice_single_plate(tmp_path, hermetic):
    res = u1_arrange.arrange_slice(
        [tmp_path / "a.stl", tmp_path / "b.stl"], tmp_path / "out",
        tool="T0", material="PLA", profile="0.20 Standard",
        runner=_runner_writing(1),
    )
    assert res["plate_count"] == 1
    assert res["plates"][0]["plate_idx"] == 1
    assert res["plates"][0]["gcode_hash"].startswith("sha256:")
    assert res["plates"][0]["metadata"]["filament_type"] == "PLA"
    assert hermetic["calls"] == [("plate_1.gcode", 0)]  # T0 -> index 0


def test_arrange_slice_multi_plate_sorted(tmp_path, hermetic):
    res = u1_arrange.arrange_slice(
        [tmp_path / f"p{i}.stl" for i in range(9)], tmp_path / "out",
        tool="T1", material="PLA", profile="0.20 Standard",
        runner=_runner_writing(3),
    )
    assert res["plate_count"] == 3
    assert [p["plate_idx"] for p in res["plates"]] == [1, 2, 3]
    # T1 -> index 1, rewritten on every plate
    assert hermetic["calls"] == [("plate_1.gcode", 1), ("plate_2.gcode", 1), ("plate_3.gcode", 1)]


def test_arrange_slice_nonzero_rc_raises(tmp_path, hermetic):
    with pytest.raises(RuntimeError, match="rc=206"):
        u1_arrange.arrange_slice(
            [tmp_path / "a.stl"], tmp_path / "out",
            tool="T0", material="PLA", profile="0.20 Standard",
            runner=_runner_writing(0, rc=206),
        )


def test_arrange_slice_empty_parts_raises(tmp_path, hermetic):
    with pytest.raises(ValueError, match="at least one STL"):
        u1_arrange.arrange_slice(
            [], tmp_path / "out",
            tool="T0", material="PLA", profile="0.20 Standard",
            runner=_runner_writing(1),
        )


def test_arrange_slice_clears_stale_plates_before_slicing(tmp_path, hermetic):
    # Review fix: a stale plate from a prior slice must be cleared so it can't
    # leak into this result. Here a prior run left plate_1 AND plate_2; the new
    # slice produces only plate_1 — the result must be EXACTLY [1], not [1, 2].
    out = tmp_path / "out"
    out.mkdir()
    (out / "plate_1.gcode").write_text("; stale 1\n")
    (out / "plate_2.gcode").write_text("; stale 2\n")
    res = u1_arrange.arrange_slice(
        [tmp_path / "a.stl"], out,
        tool="T0", material="PLA", profile="0.20 Standard",
        runner=_runner_writing(1),  # new slice yields a single plate
    )
    assert [p["plate_idx"] for p in res["plates"]] == [1]
    assert not (out / "plate_2.gcode").exists()  # stale plate gone
