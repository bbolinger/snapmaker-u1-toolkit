# Contributing

Single-maintainer project but PRs are welcome — especially from other U1 owners
who've worked out edge cases the existing scripts don't cover.

> **Working with an AI assistant on this repo?** Read [docs/DESIGN-CONTRACT.md](docs/DESIGN-CONTRACT.md) **before** touching the skill, the workflow, or the start gate. It's the single source of truth for what the system MUST do — short, opinionated, and the place to resolve "what was this supposed to do again?" The SKILL.md and scripts implement it; they don't override it.

## Setup

```bash
git clone https://github.com/bbolinger/snapmaker-u1-toolkit.git
cd snapmaker-u1-toolkit

python3 -m venv .venv
source .venv/bin/activate
pip install pytest                  # always
pip install Pillow numpy            # only if you'll touch the thumbnail tool
```

## Running the tests

```bash
pytest                              # full suite (~30s; the two Orca-on-alpine tests fail without a real Orca binary — expected)
pytest tests/test_u1_toolmap.py     # one file
pytest -k "thumbnail"               # by keyword
```

Tests use mocked Moonraker responses — no printer needed. PIL/numpy tests
`importorskip` if those packages aren't installed, so the safety-script test
subset works in stdlib-only environments.

## Conventions

- **Safety scripts (`scripts/`)** — stdlib only. No PIL, numpy, requests, etc.
  These are the scripts that run on the print pipeline; minimum deps = fewest
  ways to break the operator's environment.
- **Tools (`tools/`)** — third-party deps OK, but document them in the README
  and in the tool's docstring. Use `pytest.importorskip` in matching tests.
- **Profiles (`profiles/`)** — as of v1.5.0 this dir is per-user and
  `.gitignore`'d. Three subdirs (`from-printer/`, `user/`, `snapmaker-stock/`)
  are populated locally via `tools/extract_profiles_from_printer.py` and
  `tools/fetch_snapmaker_profiles.py`. **Don't commit JSONs to `profiles/`.**
  For PR-able contributions:
  - **Bug fixes / new filters / U1-only logic** in `tools/fetch_snapmaker_profiles.py` or `tools/extract_profile_from_gcode.py`
  - **New picker semantics** (e.g. new annotation field) in `scripts/u1_profile_picker.py` + tests
  - **Worked-example profiles** for handwriting reference go in `examples/profiles/` (MIT-licensed); see the existing `community_*.json` shape there.
- **Paths** — never hardcode `/opt/data/...`. Use `u1_config.get_data_dir()`
  for runtime state and `__file__`-relative for sibling-script `exec` targets.
  See [`scripts/u1_config.py`](scripts/u1_config.py) for the 3-tier
  data-dir resolution.
- **Lazy config** — never call `u1_config.get_u1_host()` at module import
  time. Use lazy helper functions inside `main()` or argparse defaults of
  `None` resolved post-`parse_args()`. There's a regression test that imports
  every script with no env/config to enforce this
  (`tests/test_u1_config.py::test_scripts_import_without_any_config`).
- **Atomic writes** — long-running state files (`print_history.json` etc.)
  use `tempfile.mkstemp + os.replace` (see `u1_print_history.write_json`)
  so concurrent cron runs can't produce half-written files.

## Safety model

This repo's design center is the gated-write progression
`read → slice → upload (print=false) → operator-approved start → quiet monitor`.
Don't add scripts that start, resume, cancel, or send movement/heating commands
without an explicit operator-approval gate. See the "Safety model" section in
the README for the allowed/gated breakdown.

## Pull-request checklist

Before submitting:

- [ ] `pytest` — all tests pass
- [ ] New behavior has a regression test (especially anything that bypasses
      or weakens an existing safety gate — those need the test to PROVE the
      gate still denies)
- [ ] Path / config / host references go through the helpers, not hardcoded
- [ ] If you added PIL/numpy/etc. to a script in `scripts/`, move it to `tools/`
      and document the dep instead

## Maintainer: promote a tag to a GitHub Release

`git push origin vX.Y.Z` creates a Tag on GitHub but NOT a Release object — the Releases page won't see it until a Release is explicitly created. `tools/create_release_from_tag.py` closes that gap by reading the tag's commit message and publishing it as the Release notes.

```
# After tagging + pushing a new version:
export GITHUB_TOKEN=<your PAT with `repo` scope>

python3 tools/create_release_from_tag.py                # promote the latest tag
python3 tools/create_release_from_tag.py v1.4.5         # promote a specific tag
python3 tools/create_release_from_tag.py --all-missing  # backfill every tag without a Release
python3 tools/create_release_from_tag.py v1.4.5 --update  # replace existing Release's notes
```

Idempotent: a tag that already has a Release is skipped unless `--update` is passed. Repo slug is auto-detected from `git remote get-url origin`. Token sources (first match wins): `--token`, `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PAT`.

## Reporting bugs

Open an issue with: U1 firmware version, OS + Python version, the script and
exact arguments you ran, and the JSON output (or stderr trace if it crashed).
For safety-gate bugs (e.g. "the toolmap gate let me upload with wrong
material loaded"), please include enough detail to reproduce — those are
P0 and we'll prioritize them.
