"""fetch_monitor retries a truncated / transient camera read instead of
aborting a gated start. Live 2026-07-18: the U1 camera returned a frame 487
bytes short of its declared length (http.client.IncompleteRead) during a kit
drill, so _capture_bed_and_issue_token failed and the start gate refused a real
print. A single short frame must not do that; persistent failure still raises so
the caller stays fail-closed.
"""
from __future__ import annotations

from http.client import IncompleteRead

import pytest

import u1_camera

_SOI = b"\xff\xd8\xff"
_EOI = b"\xff\xd9"
_COMPLETE = _SOI + b"\x00" * 4000 + _EOI      # full JPEG: SOI .. EOI
_TRUNCATED = _SOI + b"\x00" * 4000            # missing the EOI marker


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(u1_camera.time, "sleep", lambda *a, **k: None)


def _seq_http_get(monkeypatch, results):
    """Stub http_get to return/raise `results` in order (last item repeats)."""
    calls = {"n": 0}

    def fake(url, timeout=10.0):
        r = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(u1_camera, "http_get", fake)
    return calls


def test_looks_complete_jpeg():
    assert u1_camera._looks_complete_jpeg(_COMPLETE)
    assert not u1_camera._looks_complete_jpeg(_TRUNCATED)       # no EOI
    assert not u1_camera._looks_complete_jpeg(_SOI + _EOI)      # too small
    assert not u1_camera._looks_complete_jpeg(b"nope" * 500)    # not a JPEG


def test_retries_after_incomplete_read(tmp_path, monkeypatch):
    calls = _seq_http_get(monkeypatch, [IncompleteRead(b"x" * 94992), _COMPLETE])
    out = tmp_path / "bed.jpg"
    res = u1_camera.fetch_monitor("h", 7125, str(out))
    assert calls["n"] == 2                          # first raised, retry won
    assert res["bytes"] == len(_COMPLETE) and res["jpeg_magic"]
    assert out.read_bytes() == _COMPLETE


def test_retries_a_truncated_frame_without_exception(tmp_path, monkeypatch):
    calls = _seq_http_get(monkeypatch, [_TRUNCATED, _TRUNCATED, _COMPLETE])
    out = tmp_path / "bed.jpg"
    res = u1_camera.fetch_monitor("h", 7125, str(out))
    assert calls["n"] == 3
    assert out.read_bytes() == _COMPLETE


def test_raises_after_persistent_incomplete_read(tmp_path, monkeypatch):
    _seq_http_get(monkeypatch, [IncompleteRead(b"x" * 10)])   # always raises
    with pytest.raises(IncompleteRead):
        u1_camera.fetch_monitor("h", 7125, str(tmp_path / "bed.jpg"), attempts=3)


def test_raises_on_persistent_truncation(tmp_path, monkeypatch):
    _seq_http_get(monkeypatch, [_TRUNCATED])                  # never a full frame
    with pytest.raises(ValueError):
        u1_camera.fetch_monitor("h", 7125, str(tmp_path / "bed.jpg"), attempts=2)
