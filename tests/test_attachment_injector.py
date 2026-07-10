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


def test_space_dollar_split_mangle_is_fully_stripped(attach_dir, tmp_path):
    """The exact live 2026-07-09 reprint failure: gemma emitted
    'requests/ $u1_..._8dfe85/bed_snapshot.jpg' which whitespace-splits into a
    bare '/snapmaker_u1/requests/' prefix plus a '$..._8dfe85/bed_snapshot.jpg'
    tail. Neither fragment may survive; the real bed photo is attached instead."""
    bed = _png(tmp_path, "bed_snapshot.jpg")  # authoritative, in /tmp (no U1 dir sig)
    wf._arm_pending_attach("u1_2026_0709_8dfe85", [bed], "brent")
    reply = (
        "2\n"
        "/opt/data/snapmaker_u1/requests/ $u1_2026_0709_8dfe85/bed_snapshot.jpg\n"
        "Sliced plate, review doc, and a fresh bed photo are attached. "
        "Reply YES to start."
    )
    out = ai.transform(response_text=reply, session_id="s")
    assert out is not None
    assert bed in out, "real bed photo injected"
    assert "$u1_2026_0709_8dfe85" not in out, "mangled tail stripped"
    assert "snapmaker_u1/requests" not in out, "mangled dir prefix stripped too"
    assert "Reply YES to start." in out, "prose kept"


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


def test_review_doc_injected_as_bare_path(attach_dir, tmp_path):
    """The review .md must be injected as a BARE path so core send_document's it.
    The model's live failure was a MEDIA: directive on a mangled path, which core
    drops for .md. Assert the bare doc path is injected and the mangled MEDIA line
    (keyword + path) is gone."""
    bed = _png(tmp_path, "bed_snapshot.jpg")
    review = tmp_path / "review.md"
    review.write_text("# Print review\n- part 1\n")
    wf._arm_pending_attach("u1_2026_0709_abccb7", [bed], "brent",
                           documents=[str(review)])
    reply = (
        "Plate and bed below.\n"
        "MEDIA: /opt/data/snapmaker_u1/requests/u2026_0709_abccb7_review.md\n"
        "Reply YES."
    )
    out = ai.transform(response_text=reply, session_id="s")
    assert out is not None
    assert bed in out, "bed photo injected"
    assert str(review) in out, "review doc injected as a bare path"
    assert "u2026_0709_abccb7_review.md" not in out, "mangled doc path stripped"
    assert "MEDIA:" not in out, "orphaned MEDIA directive keyword removed"


def test_documents_only_marker_still_injects(attach_dir, tmp_path):
    """A marker with no images but a document still delivers the document."""
    review = tmp_path / "review.md"
    review.write_text("# review\n")
    wf._arm_pending_attach("u1_2026_0709_abccb7", [], "brent",
                           documents=[str(review)])
    out = ai.transform(response_text="Review attached. Reply YES.", session_id="s")
    assert out is not None
    assert str(review) in out


def test_corrupt_marker_never_raises(attach_dir):
    target = ai._marker_path_for_session()
    target.write_text("{ this is not json")
    # Must degrade to leaving the reply unchanged, not raise.
    out = ai.transform(response_text="Reply YES.", session_id="s")
    assert out is None


def test_arm_is_noop_with_no_images(attach_dir):
    wf._arm_pending_attach("u1_2026_0709_abccb7", [], "op")
    assert not list(attach_dir.iterdir()), "no marker when there's nothing to attach"
