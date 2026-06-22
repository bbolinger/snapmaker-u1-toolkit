#!/usr/bin/env python3
"""Regenerate profiles/machine/snapmaker_u1_0_4_nozzle.json by flattening
the upstream OrcaSlicer Snapmaker vendor profile chain into one standalone
JSON. Use when upstream Orca ships a new U1 profile and you want to refresh
the bundled copy.

Usage:
    # Point at your extracted OrcaSlicer install
    python3 tools/regenerate_machine_profile.py \\
        --orca-resources ~/orcaslicer-install/squashfs-root/resources/profiles

The script:
- Walks the inheritance chain starting from "Snapmaker U1 (0.4 nozzle)"
  (Snapmaker U1 -> fdm_U1 -> fdm_toolchanger -> fdm_klipper)
- Merges fields bottom-up (later/child entries override earlier/parent)
- Drops `inherits` (the output is standalone)
- Rewrites the bundled file in place + prints a summary

Pure stdlib — no PIL, no numpy needed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_TARGET = "Snapmaker U1 (0.4 nozzle)"
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "profiles" / "machine" / "snapmaker_u1_0_4_nozzle.json"


def find_profile(name: str, search_roots: list[Path]) -> Path | None:
    for root in search_roots:
        for p in root.rglob(f"{name}.json"):
            return p
    return None


def chain(name: str, roots: list[Path]) -> list[Path]:
    """Return the inheritance chain leaf-last (parent first, then child)."""
    p = find_profile(name, roots)
    if p is None:
        raise SystemExit(f"could not find profile '{name}' under {[str(r) for r in roots]}")
    data = json.loads(p.read_text(encoding="utf-8"))
    parent = data.get("inherits", "").strip()
    return (chain(parent, roots) if parent else []) + [p]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--orca-resources", type=Path, required=True,
                    help="Path to OrcaSlicer's resources/profiles directory "
                         "(e.g. ~/orcaslicer-install/squashfs-root/resources/profiles).")
    ap.add_argument("--target", default=DEFAULT_TARGET,
                    help=f"Profile name to flatten. Default: {DEFAULT_TARGET!r}.")
    ap.add_argument("--out", type=Path, default=OUTPUT_PATH,
                    help=f"Output path. Default: {OUTPUT_PATH!r}.")
    args = ap.parse_args(argv)

    if not args.orca_resources.exists():
        print(f"OrcaSlicer resources directory not found: {args.orca_resources}", file=sys.stderr)
        return 2

    # Search the Snapmaker vendor dir first, then the whole resources tree.
    snapmaker_dir = args.orca_resources / "Snapmaker" / "machine"
    roots = [snapmaker_dir, args.orca_resources] if snapmaker_dir.exists() else [args.orca_resources]

    files = chain(args.target, roots)
    if not files:
        print(f"empty chain for {args.target!r}", file=sys.stderr)
        return 3

    print(f"Inheritance chain ({len(files)} levels):")
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        print(f"  {d.get('name','?'):45s} <- {d.get('inherits','(root)'):30s}  ({f.stat().st_size:>5d}B)")

    flat: dict = {}
    for f in files:
        flat.update(json.loads(f.read_text(encoding="utf-8")))
    flat.pop("inherits", None)
    flat["from"] = "user"
    flat["instantiation"] = "true"
    flat["name"] = args.target

    out_text = json.dumps(flat, indent=2, sort_keys=True) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(out_text, encoding="utf-8")

    print()
    print(f"Wrote {args.out} ({args.out.stat().st_size} bytes, {len(flat)} fields)")
    print("Run `pytest tests/test_bundled_machine_profile.py` to verify the output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
