#!/usr/bin/env python3
"""Optional opt-in update notifier for snapmaker-u1-toolkit users.

The toolkit does NOT poll for updates by default. To enable daily
notifications, add a cron entry per the README's "Optional: notify me
when OrcaSlicer has an update" section. Without that cron entry,
this script is dormant.

When invoked:
1. Reads cache at ~/.cache/snapmaker-u1-toolkit/update-check.json
2. If cache is fresh (<24h), prints the cached verdict (which is empty
   when there's no update) and exits — no GitHub call.
3. Otherwise queries GitHub for OrcaSlicer's latest release and parses
   the installed `orca-slicer --help` version banner.
4. If the latest > installed, prints ONE line. Else prints nothing.
5. Writes the new cache regardless.

Why silent-when-current:
- Cron mails any stdout. "All good" emails are noise. Don't generate them.
- Cron mails any stderr. Network failures are common and not actionable.
  Swallowed; no nag.

Why daily cache:
- GitHub allows 60 unauthenticated calls/hour. A daily check is way under
  that ceiling regardless of how many machines you wire this on.
- If the cron fires every hour by accident, only one of 24 hits GitHub.

CLI flags:
  --orca-bin PATH     Override the orca-slicer binary location
  --force             Bypass the 24h cache (one-off "tell me now")
  --cache PATH        Override the cache file location
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_ORCA_BIN = Path(
    os.environ.get("ORCA_SLICER_BIN", "/opt/data/tools/orcaslicer/squashfs-root/bin/orca-slicer")
)
DEFAULT_CACHE = Path.home() / ".cache" / "snapmaker-u1-toolkit" / "update-check.json"
CACHE_TTL_SEC = 24 * 60 * 60  # 24h


def parse_version(s: str) -> tuple[int, ...]:
    """Parse '2.4.0', 'v2.4.0', '2.4' into (2,4,0). Returns () on failure."""
    s = (s or "").strip().lstrip("vV")
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", s)
    if not m:
        return ()
    return tuple(int(g) for g in m.groups() if g is not None)


def installed_orca_version(orca_bin: Path = DEFAULT_ORCA_BIN, timeout: float = 10.0) -> str | None:
    """Run `orca-slicer --help` and parse the first-line banner 'OrcaSlicer-2.4.0:'.
    Returns version string or None if the binary is missing / unparseable / errors."""
    if not orca_bin.exists():
        return None
    try:
        env = {**os.environ}
        # local-libs convention mirrors what u1_orient.orca_env() sets — if the
        # extracted-AppImage layout is present, prepend it to LD_LIBRARY_PATH so
        # the help banner actually emits instead of failing on missing libs.
        local_libs = orca_bin.resolve().parents[1] / "local-libs" / "usr" / "lib" / "x86_64-linux-gnu"
        if local_libs.exists():
            env["LD_LIBRARY_PATH"] = f"{local_libs}:{env.get('LD_LIBRARY_PATH', '')}"
        proc = subprocess.run(
            [str(orca_bin), "--help"], capture_output=True, text=True, timeout=timeout, env=env
        )
        # Search the full output (not just the first N lines) — future Orca
        # versions may prepend warnings or other preamble before the banner.
        for line in (proc.stdout + "\n" + proc.stderr).splitlines():
            m = re.search(r"OrcaSlicer-(\d+\.\d+(?:\.\d+)?)", line)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def latest_orca_release(timeout: float = 10.0) -> str | None:
    """Fetch OrcaSlicer's latest release tag from GitHub. Returns version
    string (without leading 'v') or None on any error — network, rate limit,
    or unexpected response shape. Fail-soft."""
    url = "https://api.github.com/repos/OrcaSlicer/OrcaSlicer/releases/latest"
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json", "User-Agent": "snapmaker-u1-toolkit-update-check"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        tag = data.get("tag_name", "")
        return tag.lstrip("vV") if tag else None
    except Exception:
        return None


def risk_label(current: tuple[int, ...], latest: tuple[int, ...]) -> str:
    """Heuristic risk classification. Major bumps may break CLI/profile schema;
    minor bumps usually add features; patches are bug-fixes."""
    if not current or not latest:
        return "unknown"
    if latest[0] > current[0]:
        return "major (CLI/profile schema may have changed; retest before relying)"
    if len(current) > 1 and len(latest) > 1 and latest[1] > current[1]:
        return "minor (new features possible; likely safe)"
    return "patch (bug fixes, likely safe)"


def load_cache(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass  # cache failures shouldn't break cron


def main(argv: list[str] | None = None) -> int:
    # Outer try/except wrapping the entire body — the README and docstring
    # promise this script "Never breaks your cron." Helper functions already
    # fail-soft, but a future code change in main() itself could leak a
    # traceback to stderr which cron would mail. This wrapper makes the
    # promise honest: any unexpected bug exits silently with rc=0.
    try:
        ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
        ap.add_argument("--orca-bin", type=Path, default=DEFAULT_ORCA_BIN,
                        help="Path to orca-slicer binary (env ORCA_SLICER_BIN also honored)")
        ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE,
                        help="Cache file path (default: ~/.cache/snapmaker-u1-toolkit/update-check.json)")
        ap.add_argument("--force", action="store_true",
                        help="Bypass the 24h cache and re-query GitHub now")
        args = ap.parse_args(argv)

        now = int(time.time())
        cache = load_cache(args.cache)
        age = now - int(cache.get("last_checked_at", 0))

        # Cache fresh: replay last verdict (None when there's no update)
        if not args.force and age < CACHE_TTL_SEC:
            msg = cache.get("update_message")
            if msg:
                print(msg)
            return 0

        current = installed_orca_version(args.orca_bin)
        latest = latest_orca_release()

        new_cache: dict[str, Any] = {
            "last_checked_at": now,
            "installed_version": current,
            "latest_release": latest,
            "update_message": None,
        }

        if current and latest:
            cur_tup = parse_version(current)
            lat_tup = parse_version(latest)
            if lat_tup > cur_tup:
                msg = (
                    f"OrcaSlicer {latest} available (you have {current}). "
                    f"{risk_label(cur_tup, lat_tup).capitalize()}. "
                    f"Release notes: https://github.com/OrcaSlicer/OrcaSlicer/releases/tag/v{latest}"
                )
                new_cache["update_message"] = msg
                print(msg)
        # else: probe failed (network / missing binary / unparseable). Silent.
        # We still write the cache with last_checked_at so we don't retry on every
        # invoke — the next attempt happens after CACHE_TTL_SEC.

        save_cache(args.cache, new_cache)
        return 0
    except Exception:
        # Honor the "never breaks your cron" promise. Real bugs lose visibility
        # via cron mail, but for an optional opt-in notifier that's the better
        # trade than waking users at 7am with a Python traceback.
        return 0


if __name__ == "__main__":
    sys.exit(main())
