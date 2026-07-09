"""Tests for the v2.4 structural image attachment.

The workflow (scripts/u1_kit_workflow.py `_arm_pending_attach`) and the plugin
hook (plugin/src/snapmaker_u1/hooks/attachment_injector.py) are written
independently and only meet through a marker file keyed by HERMES_SESSION_KEY.
The load-bearing test is the round trip: what the writer drops, the reader picks
up and turns into attached image paths, with the model's echoed paths (correct
OR mangled) removed so exactly the real images attach.
"""

import json
import sys
import time
from pathlib import Path

import pytest

# The plugin isn't pip-installed in the test env; put its src on the path.
_PLUGIN_SRC = Path(__file__).resolve().parent.parent / "plugin" / "src"
sys.path.insert(0, str(_PLUGIN_SRC))

from snapmaker_u1.hooks import attachment_injector as ai  # noqa: E402
import u1_kit_workflow as wf  # scripts/ is on the path via conftest  # noqa: E402

_SESSION_KEY = "telegram:987654:main"


@pytest.fixture
def attach_dir(tmp_path, monkeypatch):
    """Point both the writer and the reader at the same throwaway marker dir and
    pin a known session key."""
    d = tmp_path / "pending_attach"
    d.mkdir()
    monkeypatch.setattr(ai, "_PENDING_ATTACH_DIR", d)
    monkeypatch.setattr(wf, "_PENDING_ATTACH_DIR", d)
    monkeypatch.setenv("HERMES_SESSION_KEY", _SESSION_KEY)
    return d


def _png(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return str(p)


def test_no_marker_is_noop(attach_dir):
    assert ai.transform(response_text="Reply YES to start.", session_id="s") is None


def test_roundtrip_strips_mangled_path_and_attaches_real_ones(attach_dir, tmp_path):
    preview = _png(tmp_path, "plate_1_preview.png")
    bed = _png(tmp_path, "bed_snapshot.jpg")
    # Writer side (what the workflow does at the readiness card).
    wf._arm_pending_attach("u1_2026_0709_abccb7", [preview, bed], "op")
    assert list(attach_dir.iterdir()), "marker should exist after arming"

    # Model mangled the request id, so its echoed path points at nothing.
    reply = (
        "Here is your plate and the current bed.\n"
        "/opt/data/snapmaker_u1/requests/u2026_bad/plate_1_preview.png\n"
        "Reply YES to start the print."
    )
    out = ai.transform(response_text=reply, session_id="s")

    assert out is not None
    # Real paths injected...
    assert preview in out and bed in out
    # ...the mangled one is gone (won't leak as text).
    assert "u2026_bad" not in out
    # Prose is preserved.
    assert "Reply YES to start the print." in out
    # Marker consumed (one-shot).
    assert not list(attach_dir.iterdir()), "marker must be consumed"


def test_correct_echo_is_not_doubled(attach_dir, tmp_path):
    preview = _png(tmp_path, "plate_1_preview.png")
    wf._arm_pending_attach("u1_2026_0709_abccb7", [preview], "op")
    # Model happened to echo the correct path this time.
    reply = f"Plate ready.\n{preview}\nReply YES."
    out = ai.transform(response_text=reply, session_id="s")
    assert out is not None
    assert out.count(preview) == 1, "authoritative path must appear exactly once"


def test_inline_path_stripped_keeps_prose(attach_dir, tmp_path):
    bed = _png(tmp_path, "bed_snapshot.jpg")
    wf._arm_pending_attach("u1_2026_0709_abccb7", [bed], "op")
    reply = f"Current bed photo {bed} looks clear."
    out = ai.transform(response_text=reply, session_id="s")
    assert out is not None
    assert "looks clear." in out
    # The inline path token is removed from the prose line, then re-appended bare.
    assert out.rstrip().endswith(bed)


def test_nonexistent_images_and_no_echo_is_noop(attach_dir, tmp_path):
    missing = str(tmp_path / "gone" / "plate_1_preview.png")  # never created
    wf._arm_pending_attach("u1_2026_0709_abccb7", [missing], "op")
    reply = "Reply YES to start."
    out = ai.transform(response_text=reply, session_id="s")
    # Nothing exists to attach and nothing was echoed to strip -> leave reply be.
    assert out is None
    assert not list(attach_dir.iterdir()), "marker still consumed"


def test_stale_marker_discarded(attach_dir, tmp_path):
    preview = _png(tmp_path, "plate_1_preview.png")
    # Hand-write a marker that is older than the TTL at the hashed path.
    target = ai._marker_path_for_session()
    target.write_text(json.dumps({
        "request_id": "u1_2026_0709_old",
        "images": [preview],
        "operator": "op",
        "created_at": time.time() - (ai._PENDING_ATTACH_TTL_S + 60),
    }))
    out = ai.transform(response_text="Reply YES.", session_id="s")
    assert out is None, "a stale marker must not attach"
    assert not target.exists(), "stale marker is still consumed"


def test_keyed_by_session_key(attach_dir, tmp_path, monkeypatch):
    preview = _png(tmp_path, "plate_1_preview.png")
    # Arm under a DIFFERENT session key than the one the hook will read.
    wf._arm_pending_attach("u1_2026_0709_abccb7", [preview], "op",
                           session_key="telegram:OTHER:main")
    out = ai.transform(response_text="Reply YES.", session_id="s")
    assert out is None, "a marker for another session must not be picked up"


def test_unrelated_image_path_is_not_stripped(attach_dir, tmp_path):
    """A non-U1 image path the model happens to mention must survive; only U1
    artifacts get stripped."""
    bed = _png(tmp_path, "bed_snapshot.jpg")
    wf._arm_pending_attach("u1_2026_0709_abccb7", [bed], "op")
    reply = (
        "Reference shot /home/brent/holiday_preview.png for color.\n"
        "/opt/data/snapmaker_u1/requests/u2026_bad/plate_1_preview.png\n"
        "Reply YES."
    )
    out = ai.transform(response_text=reply, session_id="s")
    assert out is not None
    assert "/home/brent/holiday_preview.png" in out, "unrelated path must survive"
    assert "u2026_bad" not in out, "the U1 path is still stripped"
    assert bed in out, "the real bed photo is attached"


def test_corrupt_marker_never_raises(attach_dir):
    target = ai._marker_path_for_session()
    target.write_text("{ this is not json")
    # Must degrade to leaving the reply unchanged, not raise.
    out = ai.transform(response_text="Reply YES.", session_id="s")
    assert out is None


def test_arm_is_noop_with_no_images(attach_dir):
    wf._arm_pending_attach("u1_2026_0709_abccb7", [], "op")
    assert not list(attach_dir.iterdir()), "no marker when there's nothing to attach"
