"""Tests for tools/extract_profiles_from_printer.py.

Covers:
- The multi-tool metadata slice (the U1's main differentiator vs. single-tool printers)
- The Moonraker list + download flow (via mocked urlopen)
- CLI argparse + output paths
- Tool detection from G-code startup commands

No real-printer access — every HTTP call is mocked."""
from __future__ import annotations

import io
import json
import sys
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

import extract_profiles_from_printer as epfp  # noqa: E402


# ---------- _slice_to_tool ----------

def test_slice_to_tool_handles_comma_separator():
    """nozzle_temperature = 240,240,220,220 — comma-joined multi-tool record."""
    assert epfp._slice_to_tool("240,240,220,220", 0) == "240"
    assert epfp._slice_to_tool("240,240,220,220", 2) == "220"
    assert epfp._slice_to_tool("240,240,220,220", 3) == "220"


def test_slice_to_tool_handles_semicolon_separator():
    """filament_type = PETG;PETG;PLA;PLA — semicolon-joined multi-tool."""
    assert epfp._slice_to_tool("PETG;PETG;PLA;PLA", 0) == "PETG"
    assert epfp._slice_to_tool("PETG;PETG;PLA;PLA", 2) == "PLA"


def test_slice_to_tool_strips_quotes_on_quoted_csv():
    """filament_settings_id = \"Generic PETG\";\"Snapmaker PLA\" — Orca's
    quoted-CSV format for values that contain spaces."""
    v = '"Generic PETG";"Generic PETG";"Snapmaker PLA";"Snapmaker PLA"'
    assert epfp._slice_to_tool(v, 0) == "Generic PETG"
    assert epfp._slice_to_tool(v, 2) == "Snapmaker PLA"


def test_slice_to_tool_passthrough_for_single_value():
    """Single-tool values (no separator) come through unchanged."""
    assert epfp._slice_to_tool("PETG", 0) == "PETG"
    assert epfp._slice_to_tool("240", 1) == "240"


def test_slice_to_tool_out_of_bounds_falls_back_to_whole_value():
    """If tool_idx is past the end of the parts list, return the original
    value rather than crash."""
    result = epfp._slice_to_tool("PETG;PLA", 5)
    assert result == "PETG;PLA"  # no slice happened


# ---------- _slice_meta_to_tool ----------

def test_slice_meta_to_tool_only_touches_filament_keys():
    """Process keys (layer_height, walls) must NOT be sliced — they're global."""
    meta = {
        "filament_type": "PETG;PETG;PLA;PLA",
        "nozzle_temperature": "240,240,220,220",
        "layer_height": "0.16",
        "wall_loops": "6",
    }
    sliced = epfp._slice_meta_to_tool(meta, 0)
    assert sliced["filament_type"] == "PETG"
    assert sliced["nozzle_temperature"] == "240"
    assert sliced["layer_height"] == "0.16"  # untouched
    assert sliced["wall_loops"] == "6"  # untouched


# ---------- _detect_tool_index ----------

def test_detect_tool_index_finds_T1(tmp_path):
    g = tmp_path / "p.gcode"
    g.write_text("; HEADER_BLOCK_START\n; foo\n; HEADER_BLOCK_END\nT1\nG28\n")
    assert epfp._detect_tool_index(g) == 1


def test_detect_tool_index_finds_T3(tmp_path):
    g = tmp_path / "p.gcode"
    g.write_text("; comment\nG28\nM104 S240\nT3\nG1 X1\n")
    assert epfp._detect_tool_index(g) == 3


def test_detect_tool_index_defaults_to_0_when_no_T_command(tmp_path):
    g = tmp_path / "p.gcode"
    g.write_text("; no tool command\nG28\nG1 X1\n")
    assert epfp._detect_tool_index(g) == 0


def test_detect_tool_index_only_scans_first_300_lines(tmp_path):
    """T-commands DEEP in the file (mid-print toolchanges) don't count.
    Only the startup T-command matters."""
    g = tmp_path / "p.gcode"
    lines = ["; comment\n"] * 350 + ["T2\n"]
    g.write_text("".join(lines))
    assert epfp._detect_tool_index(g) == 0


# ---------- safe_basename ----------

def test_safe_basename_strips_extension_and_special_chars():
    assert epfp.safe_basename("globe light_PETG_5h56m.gcode") == "globe_light_PETG_5h56m"
    assert epfp.safe_basename("Dazzling Uusam_PETG_25m58s.gcode") == "Dazzling_Uusam_PETG_25m58s"
    assert epfp.safe_basename("foo/bar/baz.gcode") == "baz"


def test_safe_basename_falls_back_for_pure_garbage_input():
    """Empty/all-special input → 'gcode' fallback, not a crash."""
    assert epfp.safe_basename("....gcode") == "gcode"
    assert epfp.safe_basename("///.gcode") == "gcode"


# ---------- list_gcodes (mocked Moonraker) ----------

def test_list_gcodes_sorts_newest_first():
    """Moonraker returns files in arbitrary order; the listing should be
    sorted by modified timestamp, newest first."""
    response = json.dumps({
        "result": [
            {"path": "old.gcode", "modified": 100, "size": 1000},
            {"path": "newest.gcode", "modified": 300, "size": 2000},
            {"path": "middle.gcode", "modified": 200, "size": 1500},
        ]
    }).encode()
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: False
    mock_resp.read = lambda: response

    with patch.object(urllib.request, "urlopen", return_value=mock_resp):
        result = epfp.list_gcodes("192.168.1.100", 7125)

    assert [g["path"] for g in result] == ["newest.gcode", "middle.gcode", "old.gcode"]


# ---------- main CLI ----------

def test_main_list_prints_files_and_returns_0(monkeypatch, capsys, tmp_path):
    """--list should hit Moonraker, print files, NOT download anything."""
    response = json.dumps({
        "result": [
            {"path": "foo.gcode", "modified": 100, "size": 1024 * 1024},
            {"path": "bar.gcode", "modified": 200, "size": 2 * 1024 * 1024},
        ]
    }).encode()

    def fake_urlopen(url, timeout=12.0):
        mock = MagicMock()
        mock.__enter__ = lambda s: s
        mock.__exit__ = lambda s, *a: False
        mock.read = lambda: response
        return mock

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("SNAPMAKER_U1_HOST", "192.168.99.99")
    rc = epfp.main(["--list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "G-codes on 192.168.99.99:7125" in out
    assert "foo.gcode" in out and "bar.gcode" in out


def test_main_connection_error_returns_2_with_friendly_message(monkeypatch, capsys):
    """Network error → exit code 2 + helpful stderr, not a urllib traceback."""
    def fake_urlopen(url, timeout=12.0):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("SNAPMAKER_U1_HOST", "10.99.99.99")
    rc = epfp.main(["--list"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "Could not list G-codes" in err
    assert "SNAPMAKER_U1_HOST" in err  # actionable hint


def test_main_empty_printer_returns_3(monkeypatch, capsys):
    """No G-codes on printer → exit 3, not a confusing 'extracted 0' message."""
    response = json.dumps({"result": []}).encode()

    def fake_urlopen(url, timeout=12.0):
        mock = MagicMock()
        mock.__enter__ = lambda s: s
        mock.__exit__ = lambda s, *a: False
        mock.read = lambda: response
        return mock

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("SNAPMAKER_U1_HOST", "192.168.1.100")
    rc = epfp.main(["--list"])
    assert rc == 3


# ── head+tail range download + incremental refresh (A2, 2026-07-14) ──────────

class _Resp:
    def __init__(self, status, data):
        self.status = status
        self._d = data
    def read(self, n=-1):
        return self._d if (n is None or n < 0) else self._d[:n]
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_head_tail_download_concatenates_ranges(tmp_path, monkeypatch):
    """Fetch a head slice + a tail slice via Range and concatenate them — the
    geometry in the middle is never pulled."""
    seen = []

    def fake_urlopen(req, timeout=None):
        rng = req.headers.get('Range', '')
        seen.append(rng)
        return _Resp(206, b'HEAD' + b'h' * 20) if rng.startswith('bytes=0-') \
            else _Resp(206, b'TAIL' + b't' * 20)

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    dest = tmp_path / 'g.gcode'
    n = epfp.http_download_head_tail('http://x/g', dest, head_bytes=64, tail_bytes=64)
    body = dest.read_bytes()
    assert body.startswith(b'HEAD') and b'TAIL' in body
    assert n == len(body)
    assert any(r.startswith('bytes=0-') for r in seen)
    assert any(r.startswith('bytes=-') for r in seen)


def test_head_tail_download_distrusts_ignored_range(tmp_path, monkeypatch):
    """If the server ignores Range (200), don't treat the body as a settings
    tail — write head-only so the caller finds no footer and skips."""
    monkeypatch.setattr(urllib.request, 'urlopen',
                        lambda req, timeout=None: _Resp(200, b'X' * 30))
    dest = tmp_path / 'g.gcode'
    epfp.http_download_head_tail('http://x/g', dest, head_bytes=16, tail_bytes=16)
    assert dest.stat().st_size == 16  # head-only, not both slices


def test_refresh_from_printer_incremental_skip(tmp_path, monkeypatch):
    """Skips prints already extracted, tail-fetches only the new ones."""
    out = tmp_path / 'from-printer'
    out.mkdir()
    (out / (epfp.safe_basename('g1.gcode') + '_process.json')).write_text('{}')
    monkeypatch.setattr(epfp, 'list_gcodes',
                        lambda h, p, timeout=12.0: [{'path': 'g1.gcode'}, {'path': 'g2.gcode'}])
    seen = {}

    def fake_extract_one(host, port, name, output_dir, *, tail_bytes=None, **k):
        seen['tail_bytes'] = tail_bytes
        (output_dir / (epfp.safe_basename(name) + '_process.json')).write_text('{}')
        return {'ok': True}

    monkeypatch.setattr(epfp, 'extract_one', fake_extract_one)
    summary = epfp.refresh_from_printer(host='h', port=1, output_dir=out, limit=5)
    assert summary['skipped'] == 1          # g1 already had a profile
    assert summary['extracted'] == ['g2']   # only g2 fetched
    assert seen['tail_bytes']               # fetched tail-only, not the full file
