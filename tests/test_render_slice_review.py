from __future__ import annotations
import numpy as np
import pytest
from pathlib import Path
from u1_orient import write_binary_stl
from render_slice_review import first_layer_bbox, render_slice_review, supports_tier, tier_index, pick_recommended_orient

def test_first_layer_bbox_from_gcode(tmp_path):
    g=tmp_path/'a.gcode'; g.write_text('G1 Z0.2\nG1 X1 Y2 E0.1\nG1 X5 Y6 E0.2\nG1 Z1.0\nG1 X99 Y99 E0.3\n')
    assert first_layer_bbox(g)==(1.0,5.0,2.0,6.0)

def test_render_slice_review_outputs_png(tmp_path):
    stl=tmp_path/'a.stl'; write_binary_stl(stl, np.array([[[0,0,0],[10,0,0],[0,10,1]]], dtype=np.float32))
    g=tmp_path/'a.gcode'; g.write_text('G1 Z0.2\nG1 X0 Y0\nG1 X10 Y10\n')
    out=tmp_path/'review.png'; res=render_slice_review(stl,out,gcode=g)
    assert out.exists() and out.stat().st_size > 1000
    assert res['first_layer_bbox']==(0.0,10.0,0.0,10.0)


# ---------- supports_tier ----------

@pytest.mark.parametrize("pct,expected", [
    (0.0,    "low"),         # zero overhang
    (4.99,   "low"),         # just under low/moderate boundary
    (5.0,    "moderate"),    # at boundary
    (14.99,  "moderate"),
    (15.0,   "heavy"),
    (29.99,  "heavy"),
    (30.0,   "very heavy"),
    (100.0,  "very heavy"),  # upper bound
    (-10.0,  "low"),         # negative clamps to 0
])
def test_supports_tier_boundaries(pct, expected):
    assert supports_tier(pct) == expected


def test_supports_tier_is_monotonic():
    # Walk the input range and confirm tier index never decreases.
    order = {"low": 0, "moderate": 1, "heavy": 2, "very heavy": 3}
    last = -1
    for p in np.linspace(0, 100, 201):
        idx = order[supports_tier(float(p))]
        assert idx >= last, f"tier regressed at p={p}"
        last = idx


def test_render_slice_review_emits_overhang_stats(tmp_path):
    # Two-triangle "L-bracket": one upward face + one downward face.
    # Equal areas → overhang_area_pct ≈ 50%, supports_tier='very heavy'.
    tris = np.array([
        [[0,0,0],[10,0,0],[10,10,0]],   # downward (z normal -1)
        [[0,0,1],[10,10,1],[10,0,1]],   # upward (z normal +1)
    ], dtype=np.float32)
    stl=tmp_path/'l.stl'; write_binary_stl(stl, tris)
    out=tmp_path/'review.png'; res=render_slice_review(stl,out)
    assert res['overhang_faces'] == 1
    assert res['total_faces'] == 2
    assert 49.0 <= res['overhang_area_pct'] <= 51.0
    assert res['supports_tier'] == "very heavy"


def test_tier_index_orders_severity():
    # supports_tier output must be strictly monotonic under tier_index —
    # the workflow's "recommend lower-tier orientation" logic relies on this.
    assert tier_index("low") < tier_index("moderate") < tier_index("heavy") < tier_index("very heavy")


def test_tier_index_unknown_raises():
    # A typo in the workflow must NOT silently decay to 'low'.
    with pytest.raises(ValueError):
        tier_index("super heavy")


# ---------- pick_recommended_orient ----------

def test_pick_recommended_orient_flips_when_source_strictly_lower():
    # as-authored beats auto-orient by a clear margin → flip the default.
    orient, reason = pick_recommended_orient("low", "very heavy")
    assert orient == "asauthored"
    assert reason  # non-empty explanation


def test_pick_recommended_orient_keeps_auto_on_tie():
    # Equal tiers → keep auto, since Orca optimizes more than just supports.
    orient, reason = pick_recommended_orient("moderate", "moderate")
    assert orient == "auto"
    assert reason


def test_pick_recommended_orient_keeps_auto_when_auto_wins():
    # Auto strictly lower → obviously keep auto.
    orient, reason = pick_recommended_orient("very heavy", "low")
    assert orient == "auto"
    assert reason


def test_pick_recommended_orient_smallest_gap_flips():
    # Boundary case: adjacent tiers. Off-by-one in the comparison would
    # let auto win this; the strict `<` keeps as-authored.
    orient, _ = pick_recommended_orient("low", "moderate")
    assert orient == "asauthored"


def test_render_slice_review_clean_part_is_low_tier(tmp_path):
    # Tall thin "matchstick" with only a small downward face at the bottom.
    # Side walls dominate area; bottom triangle is a sliver → 'low' tier.
    tris = np.array([
        [[0,0,0],[1,0,0],[0,1,0]],        # bottom (small, downward)
        [[0,0,0],[1,0,0],[0,0,100]],      # side wall (large, horizontal normal)
        [[1,0,0],[0,1,0],[1,0,100]],      # side wall (large, horizontal normal)
        [[0,1,0],[0,0,0],[0,1,100]],      # side wall (large, horizontal normal)
    ], dtype=np.float32)
    stl=tmp_path/'m.stl'; write_binary_stl(stl, tris)
    out=tmp_path/'review.png'; res=render_slice_review(stl,out)
    # Bottom triangle area = 0.5, total > 150 → overhang_area_pct < 1%
    assert res['overhang_area_pct'] < 5.0
    assert res['supports_tier'] == "low"
