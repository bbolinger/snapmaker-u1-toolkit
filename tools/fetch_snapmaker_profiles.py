#!/usr/bin/env python3
"""Fetch Snapmaker U1 stock OrcaSlicer profiles from the upstream GitHub repo.

Pulls machine, process, and filament profiles from
`Snapmaker/OrcaSlicer:resources/profiles/Snapmaker/{machine,process,filament}/`
and lands U1-specific JSONs in `profiles/snapmaker-stock/` (relative to repo root,
or whatever --output-dir points at).

Why this script exists: the toolkit doesn't bundle Snapmaker's profiles —
they're upstream IP and they update. Running this script gives any U1 user
the current stock baseline. Re-run to pick up upstream updates.

Filters applied:
- machine/: only files whose name starts with "Snapmaker U1"
- process/: only files whose name contains "@Snapmaker U1" (case-sensitive
  — Snapmaker also publishes A250/A350/J1/Artisan profiles we skip)
- filament/: only files whose name contains "@U1" (the U1-specific tuned
  variants; the generic @base/@base2 files are also kept for inheritance)

Skipped:
- Files ending in `copy.json` or `_old.json` — upstream dev/staging detritus

Pure stdlib (urllib only). No auth required — Snapmaker/OrcaSlicer is public.

Example:
    python3 tools/fetch_snapmaker_profiles.py
    python3 tools/fetch_snapmaker_profiles.py --output-dir profiles/snapmaker-stock
    python3 tools/fetch_snapmaker_profiles.py --dry-run    # preview what would be downloaded
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO = "Snapmaker/OrcaSlicer"
BRANCH = "main"
UPSTREAM_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/resources/profiles/Snapmaker"
LISTING_BASE = f"https://api.github.com/repos/{REPO}/contents/resources/profiles/Snapmaker"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "profiles" / "snapmaker-stock"


def _http_json(url: str, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "snapmaker-u1-toolkit-fetcher"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_download(url: str, dest: Path, timeout: float = 60.0) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": "snapmaker-u1-toolkit-fetcher"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with urllib.request.urlopen(req, timeout=timeout) as r, dest.open("wb") as f:
        while True:
            chunk = r.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
    return written


def _list_dir(subdir: str) -> list[str]:
    """Return filenames under upstream Snapmaker/<subdir>/."""
    entries = _http_json(f"{LISTING_BASE}/{subdir}")
    if not isinstance(entries, list):
        raise RuntimeError(f"unexpected listing payload for {subdir}: {type(entries).__name__}")
    return [e["name"] for e in entries if e.get("type") == "file"]


def is_u1_machine(name: str) -> bool:
    if name.endswith("_old.json") or name.endswith("copy.json"):
        return False
    return name.startswith("Snapmaker U1") and name.endswith(".json")


def is_u1_process(name: str) -> bool:
    if name.endswith("_old.json") or name.endswith("copy.json"):
        return False
    return "@Snapmaker U1" in name and name.endswith(".json")


def is_u1_filament(name: str) -> bool:
    if name.endswith("_old.json") or name.endswith("copy.json"):
        return False
    if not name.endswith(".json"):
        return False
    # Keep U1-tuned variants AND @base inheritance bases (Orca needs them for
    # filaments that resolve via inherits). Both flavors land in one dir; the
    # workflow's profile picker filters again.
    return "@U1" in name or name.endswith("@base.json") or name.endswith("@base2.json")


CATEGORIES = (
    ("machine", is_u1_machine),
    ("process", is_u1_process),
    ("filament", is_u1_filament),
)


def fetch_all(output_dir: Path, *, dry_run: bool = False, verbose: bool = True) -> dict[str, Any]:
    """Walk machine/process/filament, filter to U1-relevant files, download
    each. Returns a summary dict suitable for printing."""
    summary: dict[str, Any] = {"output_dir": str(output_dir), "downloaded": [], "skipped": [], "errors": []}

    for subdir, keep in CATEGORIES:
        try:
            names = _list_dir(subdir)
        except Exception as e:
            summary["errors"].append({"subdir": subdir, "stage": "list", "error": f"{type(e).__name__}: {e}"})
            continue

        for name in names:
            if not keep(name):
                summary["skipped"].append(f"{subdir}/{name}")
                continue
            url = f"{UPSTREAM_BASE}/{subdir}/{urllib.parse.quote(name)}"
            dest = output_dir / subdir / name
            if dry_run:
                summary["downloaded"].append({"subdir": subdir, "name": name, "url": url, "dest": str(dest), "bytes": None})
                if verbose:
                    print(f"[dry-run] {subdir}/{name}")
                continue
            try:
                written = _http_download(url, dest)
                summary["downloaded"].append({"subdir": subdir, "name": name, "url": url, "dest": str(dest), "bytes": written})
                if verbose:
                    print(f"  {subdir}/{name}  ({written} B)")
            except Exception as e:
                summary["errors"].append({"subdir": subdir, "name": name, "stage": "download",
                                          "error": f"{type(e).__name__}: {e}"})
                if verbose:
                    print(f"  ! {subdir}/{name}  failed: {e}", file=sys.stderr)

    return summary


def write_attribution(output_dir: Path) -> None:
    """Drop a README.md next to the downloaded profiles documenting their origin
    and license."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "README.md").write_text(
        "# Snapmaker U1 stock profiles\n"
        "\n"
        f"Fetched from upstream: https://github.com/{REPO}/tree/{BRANCH}/resources/profiles/Snapmaker\n"
        "\n"
        "These are Snapmaker's official OrcaSlicer profiles for the U1 (machine,\n"
        "process, and filament JSONs). They are licensed by Snapmaker under the\n"
        "terms of their OrcaSlicer fork (see upstream LICENSE).\n"
        "\n"
        "Do not edit files in this directory directly — they will be overwritten\n"
        "the next time you run `python3 tools/fetch_snapmaker_profiles.py`.\n"
        "\n"
        "If you want your own tweaks, copy the file to `profiles/user/` and edit\n"
        "the copy. The workflow's profile picker scans all three sources\n"
        "(extracted-from-printer, snapmaker-stock, user) with extracted-first\n"
        "priority.\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                    help=f"Where to write the profiles. Default: {DEFAULT_OUTPUT_DIR}")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be downloaded, don't actually fetch.")
    ap.add_argument("--quiet", action="store_true", help="Suppress per-file output.")
    a = ap.parse_args(argv)

    print(f"Fetching Snapmaker U1 stock profiles from {REPO}@{BRANCH}")
    print(f"Destination: {a.output_dir}\n")

    summary = fetch_all(a.output_dir, dry_run=a.dry_run, verbose=not a.quiet)

    if not a.dry_run and summary["downloaded"]:
        write_attribution(a.output_dir)

    print(f"\nDownloaded: {len(summary['downloaded'])}  Skipped: {len(summary['skipped'])}  Errors: {len(summary['errors'])}")
    if summary["errors"]:
        print("\nErrors:")
        for e in summary["errors"]:
            print(f"  - {e}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
