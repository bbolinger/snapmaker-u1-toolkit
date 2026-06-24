#!/usr/bin/env python3
"""Promote a git tag to a proper GitHub Release with the commit message as
release notes.

Pushing a tag with `git push origin vX.Y.Z` creates a Tag on GitHub but NOT
a Release object — the Releases page on the repo doesn't see it until a
Release is explicitly created. This script closes that gap.

Usage:
  python3 tools/create_release_from_tag.py                      # latest tag
  python3 tools/create_release_from_tag.py v1.4.5               # specific tag
  python3 tools/create_release_from_tag.py --all-missing        # every tag without a Release
  python3 tools/create_release_from_tag.py v1.4.5 --draft       # create as draft
  python3 tools/create_release_from_tag.py v1.4.5 --prerelease  # mark prerelease

Repository owner/name is auto-detected from `git remote get-url origin`.

GitHub token sources (first match wins):
  1. --token CLI arg
  2. GITHUB_TOKEN env var
  3. GH_TOKEN env var
  4. GITHUB_PAT env var

Release name comes from the commit's subject line (first line of the
message). Release body comes from the commit's body (everything after the
blank line). Both are pulled from the *commit the tag points at*, not the
tag annotation itself — annotation messages are typically a single line
while commit bodies carry the full changelog.

Idempotent: if a Release already exists for the tag, the script reports it
and exits 0 without re-creating. Use `--update` to replace the existing
release notes with the current commit message.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any


def run_git(*args: str) -> str:
    """Run a git command, return stripped stdout. Raises on non-zero exit."""
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def detect_repo_slug() -> tuple[str, str]:
    """Parse `git remote get-url origin` for owner/repo. Handles both
    https://github.com/owner/repo.git and git@github.com:owner/repo.git."""
    url = run_git("remote", "get-url", "origin")
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", url)
    if not m:
        raise RuntimeError(f"can't parse owner/repo from origin url: {url!r}")
    return m.group(1), m.group(2)


def resolve_token(arg_token: str | None) -> str:
    """Find a GitHub token from CLI arg or env. Raises if none found."""
    if arg_token:
        return arg_token
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"):
        v = os.environ.get(var)
        if v:
            return v
    raise RuntimeError(
        "no GitHub token; pass --token or set GITHUB_TOKEN / GH_TOKEN / GITHUB_PAT"
    )


def tag_commit_subject_body(tag: str) -> tuple[str, str]:
    """Return (subject, body) of the commit a tag points at. Subject is the
    first line; body is everything after the blank line (may be empty)."""
    subject = run_git("log", "-1", "--format=%s", tag)
    body = run_git("log", "-1", "--format=%b", tag)
    return subject, body


def list_local_tags() -> list[str]:
    """All annotated/lightweight tags in the local repo, version-sorted."""
    out = run_git("tag", "--list", "--sort=v:refname")
    return [t for t in out.splitlines() if t.strip()]


def github_api(method: str, path: str, token: str, payload: dict | None = None,
               timeout: float = 15.0) -> tuple[int, dict[str, Any]]:
    """Call the GitHub REST API. Returns (status_code, parsed_json or {})."""
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "snapmaker-u1-toolkit-create-release-from-tag",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8")
        return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"message": body}
        return e.code, parsed


def get_release_by_tag(owner: str, repo: str, tag: str, token: str) -> dict[str, Any] | None:
    """Return the existing Release object for a tag, or None if 404."""
    status, data = github_api("GET", f"/repos/{owner}/{repo}/releases/tags/{tag}", token)
    if status == 200:
        return data
    return None


def create_release(owner: str, repo: str, tag: str, name: str, body: str,
                   token: str, draft: bool = False, prerelease: bool = False) -> dict[str, Any]:
    payload = {
        "tag_name": tag,
        "name": name,
        "body": body,
        "draft": draft,
        "prerelease": prerelease,
    }
    status, data = github_api("POST", f"/repos/{owner}/{repo}/releases", token, payload)
    if status not in (200, 201):
        raise RuntimeError(f"create failed: HTTP {status}: {data}")
    return data


def update_release(owner: str, repo: str, release_id: int, name: str, body: str,
                   token: str, draft: bool, prerelease: bool) -> dict[str, Any]:
    payload = {"name": name, "body": body, "draft": draft, "prerelease": prerelease}
    status, data = github_api("PATCH", f"/repos/{owner}/{repo}/releases/{release_id}", token, payload)
    if status != 200:
        raise RuntimeError(f"update failed: HTTP {status}: {data}")
    return data


def promote_tag(owner: str, repo: str, tag: str, token: str,
                draft: bool, prerelease: bool, do_update: bool) -> tuple[str, str | None]:
    """Create or update a Release for one tag. Returns (action, url-or-None).

    action ∈ {'created', 'updated', 'skipped'}.
    """
    subject, body = tag_commit_subject_body(tag)
    existing = get_release_by_tag(owner, repo, tag, token)
    if existing:
        if not do_update:
            return ("skipped", existing.get("html_url"))
        rel = update_release(owner, repo, existing["id"], subject, body, token, draft, prerelease)
        return ("updated", rel.get("html_url"))
    rel = create_release(owner, repo, tag, subject, body, token, draft, prerelease)
    return ("created", rel.get("html_url"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("tag", nargs="?", default=None,
                    help="Tag to promote (default: latest tag in local repo)")
    ap.add_argument("--all-missing", action="store_true",
                    help="Promote every local tag that doesn't already have a Release")
    ap.add_argument("--update", action="store_true",
                    help="If a Release for this tag already exists, replace its name+body with the current commit message")
    ap.add_argument("--draft", action="store_true", help="Create the Release as a draft")
    ap.add_argument("--prerelease", action="store_true", help="Mark the Release as a prerelease")
    ap.add_argument("--token", help="GitHub PAT (or set GITHUB_TOKEN / GH_TOKEN / GITHUB_PAT)")
    args = ap.parse_args(argv)

    token = resolve_token(args.token)
    owner, repo = detect_repo_slug()

    if args.all_missing:
        tags = list_local_tags()
        if not tags:
            print("no local tags found", file=sys.stderr)
            return 1
        any_change = False
        for t in tags:
            action, url = promote_tag(owner, repo, t, token, args.draft, args.prerelease, args.update)
            mark = {"created": "✓ created", "updated": "↻ updated", "skipped": "·  skipped (exists)"}[action]
            print(f"  {mark}  {t}  {url or ''}")
            if action != "skipped":
                any_change = True
        if not any_change:
            print("(no changes — all tags already have Releases)")
        return 0

    if not args.tag:
        tags = list_local_tags()
        if not tags:
            print("no local tags found", file=sys.stderr)
            return 1
        args.tag = tags[-1]
        print(f"(using latest tag: {args.tag})")

    action, url = promote_tag(owner, repo, args.tag, token, args.draft, args.prerelease, args.update)
    mark = {"created": "✓ created", "updated": "↻ updated", "skipped": "·  skipped (exists)"}[action]
    print(f"{mark}  {args.tag}  {url or ''}")
    if action == "skipped":
        print(f"  (use --update to replace the existing Release's name+body)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        # User-facing CLI tool — surface our own errors as a single line,
        # not a Python traceback (e.g., missing token, can't parse remote).
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        # `git` invocation failed — surface the git stderr if any.
        msg = (e.stderr or "").strip() or f"git {' '.join(e.cmd[1:])} failed (exit {e.returncode})"
        print(f"error: {msg}", file=sys.stderr)
        sys.exit(1)
