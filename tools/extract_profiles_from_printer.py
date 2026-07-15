#!/usr/bin/env python3
"""Pull recent G-codes from a U1 over Moonraker and extract Snapmaker Orca
process + filament profiles from each.

End-to-end one-shot for "I want profiles derived from MY printer's history,
not the example community profiles shipped in this repo." For each G-code
in the printer's `gcodes` root (newest first by default), this script:

  1. Downloads the G-code via Moonraker
  2. Parses its slicer metadata block
  3. Writes a process JSON + filament JSON to the output dir, named after
     the source G-code

Uses scripts/u1_config.py for the host/port resolution (env > .env > config
file), and reuses tools/extract_profile_from_gcode.py for the actual
metadata parsing + profile shape. Pure stdlib.

Example — pull profiles from the 5 most recent prints on the printer:

    python3 tools/extract_profiles_from_printer.py

Example — see what's available without downloading:

    python3 tools/extract_profiles_from_printer.py --list

Example — pull a specific print only, with vendor override:

    python3 tools/extract_profiles_from_printer.py \\
        --file "globe_light_PETG_5h56m.gcode" \\
        --vendor SUNLU \\
        --output-dir profiles/from-my-printer
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add scripts/ and tools/ to sys.path so we can reuse u1_config + the extractor.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

from u1_config import get_u1_host, get_u1_port  # noqa: E402
import extract_profile_from_gcode as epfg  # noqa: E402


REPO_ROOT = HERE.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "profiles" / "from-printer"
# Skip G-codes bigger than this — they're slow to download and rarely
# the operator's quick-print baselines. Override with --max-size-mb.
DEFAULT_MAX_MB = 200

# Multi-tool U1 G-code metadata records ALL 4 tool slots, separator-joined.
# Examples seen in the wild:
#   nozzle_temperature      = 240,240,220,220     (comma)
#   filament_type           = PETG;PETG;PLA;PLA   (semicolon)
#   filament_settings_id    = "Generic PETG";"Snapmaker PLA"  (quoted + semi)
# Build_filament_profile naively wraps the whole thing into a single-item list,
# producing a profile that names every tool's filament. _slice_to_tool below
# fixes this by keeping only the value at the intended tool's index.
_TOOL_NAME_TO_INDEX = {"extruder": 0, "extruder1": 1, "extruder2": 2, "extruder3": 3}


def _slice_to_tool(value: str, tool_idx: int) -> str:
    """If `value` looks multi-tool (multiple values joined by `;` or `,`),
    return just the slice for `tool_idx`. Single-tool values pass through."""
    for sep in (";", ","):
        if sep in value:
            parts = [p.strip().strip('"').strip("'") for p in value.split(sep)]
            if 0 <= tool_idx < len(parts):
                return parts[tool_idx]
    return value


def _slice_meta_to_tool(meta: dict[str, str], tool_idx: int) -> dict[str, str]:
    """Apply _slice_to_tool to every filament-side metadata field. Process-side
    fields (layer_height, walls, infill, supports) are global and untouched."""
    sliced = dict(meta)
    for key in epfg.FILAMENT_FIELDS_FROM_META + ["filament_settings_id", "first_layer_temperature", "first_layer_bed_temperature"]:
        if key in sliced:
            sliced[key] = _slice_to_tool(sliced[key], tool_idx)
    return sliced


def _detect_tool_index(gcode_path: Path) -> int:
    """Walk the G-code's first ~300 lines for a T0/T1/T2/T3 startup command
    and return that tool's index. Falls back to 0 (T0) if undetectable."""
    with gcode_path.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if i > 300:
                break
            m = re.match(r"\s*T(\d+)\b", line)
            if m:
                return int(m.group(1))
    return 0


def http_json(url: str, timeout: float = 12.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_download(url: str, dest: Path, timeout: float = 120.0) -> int:
    """Stream-download a (potentially large) file. Returns bytes written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = 0
    with urllib.request.urlopen(url, timeout=timeout) as r, dest.open("wb") as f:
        while True:
            chunk = r.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)
            bytes_written += len(chunk)
    return bytes_written


def http_download_head_tail(url: str, dest: Path, *, head_bytes: int = 262144,
                            tail_bytes: int = 1048576, timeout: float = 30.0) -> int:
    """Fetch only the HEAD + TAIL of a G-code via HTTP Range requests and write
    them (concatenated) to dest — transferring ~1 MB instead of a whole print.

    The geometry in the middle is never downloaded. The intended tool is a
    T-command in the first lines (``_detect_tool_index`` scans the first 300);
    the slicer settings are the trailing comment block
    (``parse_gcode_metadata`` seeks the last 512 KB). So a head slice covers the
    former and a tail slice the latter. Reads are BOUNDED, so a server that
    ignores Range can't make us pull the whole file — we then get a head-only
    chunk with no settings footer, which the caller skips. Returns bytes written.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _range(hdr: str, cap: int) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"Range": hdr})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return getattr(r, "status", 200), r.read(cap)
        except Exception:
            return 0, b""

    hs, head = _range(f"bytes=0-{head_bytes - 1}", head_bytes)
    ts, tail = _range(f"bytes=-{tail_bytes}", tail_bytes)
    # Only trust the tail slice when the server honored the suffix range (206);
    # otherwise `tail` is just the file start again and carries no settings
    # footer, so write head-only and let the caller find no metadata + skip.
    body = ((head if hs == 206 else b"") + tail) if ts == 206 else head
    dest.write_bytes(body)
    return len(body)


def list_gcodes(host: str, port: int, timeout: float = 12.0) -> list[dict[str, Any]]:
    """Return Moonraker's listing of files under root=gcodes, newest first.

    Each entry has keys like {path, modified, size, ...}. Sorting by modified
    works across firmware variants better than relying on uploaded ordering.
    """
    base = f"http://{host}:{port}"
    payload = http_json(f"{base}/server/files/list?root=gcodes", timeout=timeout)
    items = payload.get("result", [])
    items.sort(key=lambda f: float(f.get("modified") or 0), reverse=True)
    return items


def safe_basename(filename: str) -> str:
    """Strip directory components + file extension; turn into a safe
    filesystem-friendly slug for profile filenames."""
    stem = Path(filename).stem  # drop .gcode
    # Replace anything not alnum/_/- with underscore
    return re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:80] or "gcode"


def extract_one(
    host: str,
    port: int,
    file_path: str,
    output_dir: Path,
    *,
    vendor: str | None = None,
    brand_label: str | None = None,
    keep_gcode: bool = False,
    download_dir: Path | None = None,
    head_bytes: int = 262144,
    tail_bytes: int | None = None,
) -> dict[str, Any]:
    """Download one G-code from the printer + extract its process + filament
    profiles into output_dir. Returns a summary dict for reporting.

    When ``tail_bytes`` is set, only the G-code's head + tail are fetched via
    Range requests (settings live in the trailing comment block; the tool in the
    first lines), so a 200 MB print costs a ~1 MB transfer instead of the whole
    file. Left None (the CLI default) downloads the full file, unchanged."""
    base = f"http://{host}:{port}"
    download_dir = download_dir or output_dir / ".downloaded"
    download_dir.mkdir(parents=True, exist_ok=True)
    local = download_dir / Path(file_path).name

    url = f"{base}/server/files/gcodes/{urllib.parse.quote(file_path)}"
    if tail_bytes:
        written = http_download_head_tail(url, local, head_bytes=head_bytes,
                                          tail_bytes=tail_bytes)
    else:
        written = http_download(url, local)

    meta = epfg.parse_gcode_metadata(local)
    if not meta:
        if not keep_gcode:
            local.unlink(missing_ok=True)
        return {"file": file_path, "ok": False,
                "error": "no slicer metadata block found in G-code"}

    # Detect intended tool from the G-code's startup commands so multi-tool
    # filament metadata gets sliced to just that tool's value.
    # parse_gcode_metadata in extract_profile_from_gcode doesn't return the
    # intended tool, so re-parse via the upload script's metadata parser to
    # get the T0/T1/T2/T3 hint. Falls back to extruder/T0 if undetectable.
    tool_idx = _detect_tool_index(local)
    filament_meta = _slice_meta_to_tool(meta, tool_idx)

    slug = safe_basename(file_path)
    process_name = meta.get("print_settings_id") or f"{slug} (extracted process)"
    filament_name = filament_meta.get("filament_settings_id") or f"{slug} (extracted filament)"
    if brand_label and brand_label not in filament_name:
        filament_name = f"{filament_name} ({brand_label})"

    process = epfg.build_process_profile(meta, process_name, "Snapmaker U1 (0.4 nozzle)")
    filament = epfg.build_filament_profile(filament_meta, filament_name, "Snapmaker U1 (0.4 nozzle)", vendor=vendor)

    output_dir.mkdir(parents=True, exist_ok=True)
    process_out = output_dir / f"{slug}_process.json"
    filament_out = output_dir / f"{slug}_filament.json"
    process_out.write_text(json.dumps(process, indent=4), encoding="utf-8")
    filament_out.write_text(json.dumps(filament, indent=4), encoding="utf-8")

    if not keep_gcode:
        local.unlink(missing_ok=True)

    return {
        "file": file_path,
        "ok": True,
        "size_bytes": written,
        "process_out": str(process_out),
        "filament_out": str(filament_out),
        "tool": f"T{tool_idx}",
        "filament_type": filament_meta.get("filament_type"),  # post-slice
        "layer_height": meta.get("layer_height"),
    }


def already_extracted(output_dir: Path, file_path: str) -> bool:
    """True if this G-code's process profile is already in output_dir — the
    incremental-skip key so a spool of reprints doesn't re-fetch every time."""
    return (output_dir / f"{safe_basename(file_path)}_process.json").exists()


def refresh_from_printer(host: str | None = None, port: int | None = None,
                         output_dir: Path | None = None, *, limit: int = 5,
                         head_bytes: int = 262144, tail_bytes: int = 1048576,
                         timeout: float = 30.0, vendor: str | None = None) -> dict:
    """Incrementally populate ``profiles/from-printer/`` from the printer's most
    recent prints, fetching ONLY each G-code's head+tail (settings), never the
    geometry. Skips prints already extracted, so it is cheap enough to run inline
    at form-build. Fully GUARDED: never raises; returns a summary dict
    ``{extracted, skipped, errors}`` so a printer that's off/unreachable simply
    leaves the existing profiles in place."""
    summary: dict[str, Any] = {"extracted": [], "skipped": 0, "errors": []}
    try:
        if host is None or port is None:
            from u1_config import get_u1_host, get_u1_port
            host = host or get_u1_host()
            port = port if port is not None else get_u1_port()
        output_dir = output_dir or DEFAULT_OUTPUT_DIR
        gcodes = list_gcodes(host, int(port), timeout=timeout)
    except Exception as exc:
        summary["errors"].append(f"list: {exc}")
        return summary
    for g in gcodes[:max(0, int(limit))]:
        name = g.get("path") or g.get("filename") or g.get("name")
        if not name:
            continue
        try:
            if already_extracted(output_dir, name):
                summary["skipped"] += 1
                continue
            res = extract_one(host, int(port), name, output_dir, vendor=vendor,
                              head_bytes=head_bytes, tail_bytes=tail_bytes)
            if res.get("ok"):
                summary["extracted"].append(safe_basename(name))
            else:
                summary["errors"].append(f"{name}: {res.get('error')}")
        except Exception as exc:
            summary["errors"].append(f"{name}: {exc}")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=None,
                    help="Override SNAPMAKER_U1_HOST.")
    ap.add_argument("--port", type=int, default=None,
                    help="Override SNAPMAKER_U1_PORT (default 7125).")
    ap.add_argument("--list", action="store_true",
                    help="List G-codes available on the printer and exit; "
                         "no download or extraction.")
    ap.add_argument("--limit", type=int, default=5,
                    help="Max number of recent G-codes to extract (default 5). "
                         "Ignored if --file is set.")
    ap.add_argument("--file", default=None,
                    help="Extract a specific G-code by exact path (as shown by --list).")
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                    help=f"Where to write extracted profiles. Default: {DEFAULT_OUTPUT_DIR}.")
    ap.add_argument("--vendor", default=None,
                    help="Override filament_vendor (G-code often says 'Generic'; "
                         "set 'SUNLU', 'eSun', etc.).")
    ap.add_argument("--brand-label", default=None,
                    help="Optional human label appended to extracted filament names.")
    ap.add_argument("--max-size-mb", type=float, default=DEFAULT_MAX_MB,
                    help=f"Skip G-codes larger than this many MB. Default: {DEFAULT_MAX_MB}.")
    ap.add_argument("--keep-gcode", action="store_true",
                    help="Keep downloaded G-codes on disk (default: deleted after extraction).")
    args = ap.parse_args(argv)

    host = args.host or get_u1_host()
    port = args.port if args.port is not None else get_u1_port()

    try:
        gcodes = list_gcodes(host, port)
    except urllib.error.URLError as exc:
        print(
            f"Could not list G-codes from {host}:{port}: {exc}\n"
            "  • Check the printer is on and on your LAN.\n"
            "  • Edit .env (or set SNAPMAKER_U1_HOST), or pass --host <ip>.",
            file=sys.stderr,
        )
        return 2

    if not gcodes:
        print(f"No G-codes found on printer at {host}:{port}", file=sys.stderr)
        return 3

    if args.list:
        print(f"G-codes on {host}:{port} ({len(gcodes)} total, newest first):")
        for g in gcodes[:20]:
            mtime = datetime.fromtimestamp(float(g.get("modified") or 0), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            size_mb = (g.get("size") or 0) / (1024 * 1024)
            print(f"  {mtime}  {size_mb:>6.1f} MB  {g.get('path')}")
        if len(gcodes) > 20:
            print(f"  ... and {len(gcodes) - 20} more")
        return 0

    # Pick targets
    if args.file:
        targets = [g for g in gcodes if g.get("path") == args.file]
        if not targets:
            print(f"G-code not found on printer: {args.file!r}", file=sys.stderr)
            print("Run with --list to see available files.", file=sys.stderr)
            return 4
    else:
        # Filter by size cap
        max_bytes = args.max_size_mb * 1024 * 1024
        oversized = [g for g in gcodes if (g.get("size") or 0) > max_bytes]
        if oversized:
            names = ", ".join(g.get("path", "?") for g in oversized[:3])
            print(f"  (skipping {len(oversized)} G-code(s) over {args.max_size_mb} MB: {names}…)", file=sys.stderr)
        eligible = [g for g in gcodes if (g.get("size") or 0) <= max_bytes]
        targets = eligible[: args.limit]
        if not targets:
            print("No G-codes passed size filter; raise --max-size-mb or pick --file explicitly.",
                  file=sys.stderr)
            return 5

    print(f"Extracting profiles from {len(targets)} G-code(s) on {host}:{port} -> {args.output_dir}")
    ok_count = 0
    fail_count = 0
    for g in targets:
        result = extract_one(
            host, port, g["path"], args.output_dir,
            vendor=args.vendor, brand_label=args.brand_label,
            keep_gcode=args.keep_gcode,
        )
        if result["ok"]:
            ok_count += 1
            print(f"  ✓ {g['path']}")
            print(f"      → {Path(result['process_out']).name}")
            print(f"      → {Path(result['filament_out']).name}  "
                  f"({result.get('tool', '?')} / {result.get('filament_type', '?')}, "
                  f"layer {result.get('layer_height', '?')})")
        else:
            fail_count += 1
            print(f"  ✗ {g['path']}: {result.get('error', 'unknown error')}")

    print(f"\nDone: {ok_count} succeeded, {fail_count} failed. Output: {args.output_dir}")
    return 0 if fail_count == 0 else 6


if __name__ == "__main__":
    raise SystemExit(main())
