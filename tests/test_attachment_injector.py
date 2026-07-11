"""Tests for the v2.4 structural attachment injector.

The workflow (scripts/u1_kit_workflow.py `_arm_pending_attach`) and the plugin
hook (plugin/src/snapmaker_u1/hooks/attachment_injector.py) meet only through a
one-shot marker keyed by HERMES_SESSION_KEY. The load-bearing test is the round
trip: what the writer drops, the reader validates and turns into attachments,
with the model's echoed paths (correct OR mangled) removed so exactly the real
artifacts attach.

The marker is same-uid-writable and therefore untrusted, so the hook validates
every path (real, non-symlink, known artifact name, under the requests root)
before delivery. Several tests forge markers directly to prove those rejections.
"""

import json
import os
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
_RID = "u1_2026_0709_abccb7"


@pytest.fixture
def attach_dir(tmp_path, monkeypatch):
    """Point writer and reader at the same throwaway marker dir + requests root,
    and pin a known session key."""
    d = tmp_path / "pending_attach"
    d.mkdir()
    req_root = tmp_path / "requests"
    req_root.mkdir()
    monkeypatch.setattr(ai, "_PENDING_ATTACH_DIR", d)
    monkeypatch.setattr(wf, "_PENDING_ATTACH_DIR", d)
    monkeypatch.setattr(ai, "_REQUESTS_ROOT", req_root)
    monkeypatch.setenv("HERMES_SESSION_KEY", _SESSION_KEY)
    return d


def _artifact(tmp_path, name, rid=_RID):
    """Create a valid U1 artifact at <requests_root>/<rid>/<name>."""
    d = tmp_path / "requests" / rid
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return str(p)


def _write_marker(session_key, payload):
    """Forge a marker directly (bypassing the workflow writer) at the hook's
    session slot, to exercise validation/rejection paths."""
    digest = __import__("hashlib").sha256(session_key.encode()).hexdigest()[:16]
    p = ai._PENDING_ATTACH_DIR / f"{digest}.json"
    p.write_text(json.dumps(payload))
    return p


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_no_marker_is_noop(attach_dir):
    assert ai.transform(response_text="Reply YES to start.", session_id="s") is None


def test_roundtrip_strips_mangled_path_and_attaches_real_ones(attach_dir, tmp_path):
    preview = _artifact(tmp_path, "plate_1_preview.png")
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    wf._arm_pending_attach(_RID, [preview, bed], "op")
    assert list(attach_dir.iterdir()), "marker should exist after arming"

    reply = (
        "Here is your plate and the current bed.\n"
        "/opt/data/snapmaker_u1/requests/u2026_bad/plate_1_preview.png\n"
        "Reply YES to start the print."
    )
    out = ai.transform(response_text=reply, session_id="s")
    assert out is not None
    assert preview in out and bed in out
    assert "u2026_bad" not in out
    assert "Reply YES to start the print." in out
    assert not list(attach_dir.iterdir()), "marker must be consumed"


def test_space_dollar_split_mangle_is_fully_stripped(attach_dir, tmp_path):
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    wf._arm_pending_attach(_RID, [bed], "brent")
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
    assert "Reply YES to start." in out


def test_correct_echo_is_not_doubled(attach_dir, tmp_path):
    preview = _artifact(tmp_path, "plate_1_preview.png")
    wf._arm_pending_attach(_RID, [preview], "op")
    reply = f"Plate ready.\n{preview}\nReply YES."
    out = ai.transform(response_text=reply, session_id="s")
    assert out is not None
    assert out.count(preview) == 1, "authoritative path must appear exactly once"


def test_inline_path_stripped_keeps_prose(attach_dir, tmp_path):
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    wf._arm_pending_attach(_RID, [bed], "op")
    reply = f"Current bed photo {bed} looks clear."
    out = ai.transform(response_text=reply, session_id="s")
    assert out is not None
    assert "looks clear." in out
    assert out.rstrip().endswith(bed)


def test_review_doc_injected_as_bare_path(attach_dir, tmp_path):
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    review = _artifact(tmp_path, "review.md")
    wf._arm_pending_attach(_RID, [bed], "brent", documents=[review])
    reply = (
        "Plate and bed below.\n"
        "MEDIA: /opt/data/snapmaker_u1/requests/u2026_0709_abccb7_review.md\n"
        "Reply YES."
    )
    out = ai.transform(response_text=reply, session_id="s")
    assert out is not None
    assert bed in out and review in out
    assert "u2026_0709_abccb7_review.md" not in out
    assert "MEDIA:" not in out


def test_documents_only_marker_still_injects(attach_dir, tmp_path):
    review = _artifact(tmp_path, "review.md")
    wf._arm_pending_attach(_RID, [], "brent", documents=[review])
    out = ai.transform(response_text="Review attached. Reply YES.", session_id="s")
    assert out is not None
    assert review in out


def test_unrelated_image_path_is_not_stripped(attach_dir, tmp_path):
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    wf._arm_pending_attach(_RID, [bed], "op")
    reply = (
        "Reference shot /home/brent/holiday_preview.png for color.\n"
        "/opt/data/snapmaker_u1/requests/u2026_bad/plate_1_preview.png\n"
        "Reply YES."
    )
    out = ai.transform(response_text=reply, session_id="s")
    assert out is not None
    assert "/home/brent/holiday_preview.png" in out, "unrelated path must survive"
    assert "u2026_bad" not in out
    assert bed in out


# --------------------------------------------------------------------------- #
# Security: the marker is untrusted (audit #2)
# --------------------------------------------------------------------------- #

def test_forged_path_outside_root_refused(attach_dir, tmp_path):
    """A forged marker pointing at /etc/hosts must never be delivered."""
    _write_marker(_SESSION_KEY, {
        "request_id": _RID, "images": ["/etc/hosts"], "documents": [],
        "operator": "op", "created_at": time.time(),
    })
    out = ai.transform(response_text="Reply YES.", session_id="s")
    assert out is None, "no injectable artifact -> reply unchanged"
    assert not list(attach_dir.iterdir()), "marker still consumed"


def test_disallowed_name_under_root_refused(attach_dir, tmp_path):
    """A real file in a valid request dir but with a non-artifact name is refused."""
    secret = _artifact(tmp_path, "secret.png")  # valid location, wrong name
    _write_marker(_SESSION_KEY, {
        "request_id": _RID, "images": [secret], "documents": [],
        "operator": "op", "created_at": time.time(),
    })
    out = ai.transform(response_text="Reply YES.", session_id="s")
    assert out is None


def test_symlinked_artifact_refused(attach_dir, tmp_path):
    """A symlink named like an artifact but pointing outside is refused."""
    link = tmp_path / "requests" / _RID / "bed_snapshot.jpg"
    link.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside.jpg"
    target.write_bytes(b"x")
    os.symlink(target, link)
    _write_marker(_SESSION_KEY, {
        "request_id": _RID, "images": [str(link)], "documents": [],
        "operator": "op", "created_at": time.time(),
    })
    out = ai.transform(response_text="Reply YES.", session_id="s")
    assert out is None


def test_missing_file_refused(attach_dir, tmp_path):
    missing = str(tmp_path / "requests" / _RID / "bed_snapshot.jpg")  # never created
    _write_marker(_SESSION_KEY, {
        "request_id": _RID, "images": [missing], "documents": [],
        "operator": "op", "created_at": time.time(),
    })
    assert ai.transform(response_text="Reply YES.", session_id="s") is None


# --------------------------------------------------------------------------- #
# Timestamp validation (audit #6)
# --------------------------------------------------------------------------- #

def test_missing_timestamp_marker_rejected(attach_dir, tmp_path):
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    _write_marker(_SESSION_KEY, {"request_id": _RID, "images": [bed]})  # no created_at
    assert ai.transform(response_text="Reply YES.", session_id="s") is None


def test_string_timestamp_marker_rejected(attach_dir, tmp_path):
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    _write_marker(_SESSION_KEY, {
        "request_id": _RID, "images": [bed], "created_at": "soon"})
    assert ai.transform(response_text="Reply YES.", session_id="s") is None


def test_stale_marker_discarded(attach_dir, tmp_path):
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    target = _write_marker(_SESSION_KEY, {
        "request_id": _RID, "images": [bed],
        "created_at": time.time() - (ai._PENDING_ATTACH_TTL_S + 60)})
    assert ai.transform(response_text="Reply YES.", session_id="s") is None
    assert not target.exists(), "stale marker is still consumed"


# --------------------------------------------------------------------------- #
# Session correlation + one-shot (audit #3, #4)
# --------------------------------------------------------------------------- #

def test_empty_session_key_writes_no_marker(attach_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    wf._arm_pending_attach(_RID, [bed], "op")  # empty key -> no marker
    assert not list(attach_dir.iterdir()), "empty-key slot must not be armed"
    assert ai.transform(response_text="Reply YES.", session_id="s") is None


def test_keyed_by_session_key(attach_dir, tmp_path):
    preview = _artifact(tmp_path, "plate_1_preview.png")
    wf._arm_pending_attach(_RID, [preview], "op",
                           session_key="telegram:OTHER:main")
    out = ai.transform(response_text="Reply YES.", session_id="s")
    assert out is None, "a marker for another session must not be picked up"


def test_marker_is_single_use(attach_dir, tmp_path):
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    wf._arm_pending_attach(_RID, [bed], "op")
    first = ai.transform(response_text="Reply YES.", session_id="s")
    second = ai.transform(response_text="Reply YES.", session_id="s")
    assert first is not None and bed in first
    assert second is None, "the marker must be consumed exactly once"


def test_corrupt_marker_never_raises(attach_dir):
    digest = __import__("hashlib").sha256(_SESSION_KEY.encode()).hexdigest()[:16]
    (ai._PENDING_ATTACH_DIR / f"{digest}.json").write_text("{ this is not json")
    assert ai.transform(response_text="Reply YES.", session_id="s") is None


def test_arm_is_noop_with_no_images(attach_dir):
    wf._arm_pending_attach(_RID, [], "op")
    assert not list(attach_dir.iterdir()), "no marker when there's nothing to attach"


# --------------------------------------------------------------------------- #
# Session-key source: gateway contextvars, NOT gateway os.environ
# --------------------------------------------------------------------------- #
# Live 2026-07-11: the gateway never writes HERMES_SESSION_KEY into its own
# process env (concurrent sessions would clobber it), so the env-only read
# returned empty at transform time and the hook refused EVERY turn — the
# marker sat unconsumed while attachment silently fell back to model echo.
# The hook must read Hermes' session contextvar (gateway.session_context
# .get_session_env), the same source the u1_kit tool exports to the
# workflow subprocess that writes the marker.

def _stub_gateway_session(monkeypatch, key):
    import types
    gw = types.ModuleType("gateway")
    sc = types.ModuleType("gateway.session_context")
    sc.get_session_env = lambda name, default="": (
        key if name == "HERMES_SESSION_KEY" else default)
    monkeypatch.setitem(sys.modules, "gateway", gw)
    monkeypatch.setitem(sys.modules, "gateway.session_context", sc)


def test_contextvar_key_found_without_env(attach_dir, tmp_path, monkeypatch):
    """The gateway-process case: no env var at all, key only in contextvars."""
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    _stub_gateway_session(monkeypatch, _SESSION_KEY)
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    wf._arm_pending_attach(_RID, [bed], "op", session_key=_SESSION_KEY)
    out = ai.transform(response_text="Reply YES.", session_id="s")
    assert out is not None and bed in out
    assert not list(attach_dir.iterdir()), "marker consumed via contextvar key"


def test_contextvar_key_wins_over_env(attach_dir, tmp_path, monkeypatch):
    """A stale env value must not shadow the turn's real session key."""
    monkeypatch.setenv("HERMES_SESSION_KEY", "telegram:STALE:main")
    _stub_gateway_session(monkeypatch, _SESSION_KEY)
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    wf._arm_pending_attach(_RID, [bed], "op", session_key=_SESSION_KEY)
    out = ai.transform(response_text="Reply YES.", session_id="s")
    assert out is not None and bed in out


def test_env_fallback_when_gateway_absent(attach_dir, tmp_path):
    """Non-gateway callers (tests, subprocess side) keep the env path.
    All earlier tests in this file exercise it implicitly; this pins it."""
    bed = _artifact(tmp_path, "bed_snapshot.jpg")
    wf._arm_pending_attach(_RID, [bed], "op")
    assert ai._session_key() == _SESSION_KEY  # from the fixture's env var
    out = ai.transform(response_text="Reply YES.", session_id="s")
    assert out is not None and bed in out
