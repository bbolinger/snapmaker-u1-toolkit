# Contributing

Single-maintainer project but PRs are welcome — especially from other U1 owners
who've worked out edge cases the existing scripts don't cover.

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
pytest                              # all 105 tests (~25s — subprocess tests dominate)
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
- **Profiles (`profiles/`)** — community-derived JSONs. New profiles should
  follow the existing naming convention (`community_<preset>_<surface>.json`)
  and target the Snapmaker U1 platform.
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

## Reporting bugs

Open an issue with: U1 firmware version, OS + Python version, the script and
exact arguments you ran, and the JSON output (or stderr trace if it crashed).
For safety-gate bugs (e.g. "the toolmap gate let me upload with wrong
material loaded"), please include enough detail to reproduce — those are
P0 and we'll prioritize them.
