"""Post-upload metadata must come from a completed scan, not a racing GET.

Moonraker parses a large file's metadata asynchronously after upload; the
printer's screen can ask for the thumbnail in that window and cache the
miss. fetch_remote_metadata forces the scan via /server/files/metascan and
falls back to the plain GET on older Moonraker builds."""
from __future__ import annotations

import json

import u1_upload_gcode as up


class _Resp:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode()

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_metascan_result_wins(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        calls.append(url)
        assert "metascan" in url
        return _Resp({"result": {"size": 5, "thumbnails": [{"width": 300}]}})

    monkeypatch.setattr(up.urllib.request, "urlopen", fake_urlopen)
    meta = up.fetch_remote_metadata("h", 7125, "big_plate.gcode")
    assert meta.get("thumbnails")
    assert len(calls) == 1  # no fallback GET needed


def test_falls_back_to_get_when_metascan_absent(monkeypatch):
    def fake_urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if "metascan" in url:
            raise OSError("404")
        return _Resp({"result": {"size": 5}})

    monkeypatch.setattr(up.urllib.request, "urlopen", fake_urlopen)
    meta = up.fetch_remote_metadata("h", 7125, "big_plate.gcode")
    assert meta == {"size": 5}


def test_returns_empty_when_everything_fails(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise OSError("printer offline")

    monkeypatch.setattr(up.urllib.request, "urlopen", fake_urlopen)
    assert up.fetch_remote_metadata("h", 7125, "x.gcode") == {}
