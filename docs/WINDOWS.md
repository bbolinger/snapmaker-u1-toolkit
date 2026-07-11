# Native Windows (experimental)

The supported runtime is Linux (or WSL). Native Windows Hermes Desktop is
**experimental**: the portability layer shipped in v2.4.1 and passed its
test suite, but end-to-end operation on Windows has not been validated as
far as the Linux runtime has. Treat prints started from a Windows install
with extra care until you have run the checks below yourself.

## What works, what to use

- **Slicer:** use an upstream OrcaSlicer **portable** build (2.4.2 was
  validated). Extract the zip anywhere; the toolkit finds the bundled
  profiles next to `orca-slicer.exe` automatically. Do **not** point the
  toolkit at the Snapmaker Orca fork on Windows — its CLI slice
  segfaults (verified 2026-07-10).
- **Shell:** run installs and scripts from **Git Bash** (ships with Git
  for Windows). Raw `cmd.exe` is not supported.
- **Python:** a plain python.org 3.11+ install with
  `pip install numpy pillow`, or a project venv
  (`python -m venv venv`, `venv/Scripts/pip install numpy pillow`).

## Setup differences vs Linux

```bash
export ORCA_SLICER_BIN='C:/path/to/orca242/orca-slicer.exe'
```

Everything else resolves itself:

- The workflow scripts self-locate; emitted commands point at wherever
  the scripts actually are.
- Pending-state markers (confirm / cancel / attach) live under the
  native temp dir (`%TEMP%\u1_pending\...`), shared by every native
  process. Do not mix Git Bash's `/tmp` into custom overrides.
- If Hermes Desktop injects its own venv into `PYTHONPATH`, the
  workflow detects the poisoned environment and relaunches itself with
  it cleared (set `U1_KEEP_PYTHONPATH=1` if you genuinely need it kept).
- `adapters/hermes/install.py` understands the Windows venv layout
  (`Lib\site-packages`, `Scripts\python.exe`). Pass
  `--venv 'C:/Users/<you>/AppData/Local/hermes/hermes-agent/venv'`
  (or wherever your Hermes venv lives).
- `deploy_to_runtime.sh` deploys into `$HERMES_HOME` when `/opt/data`
  does not exist. Set `HERMES_HOME` first.

The bundled skill text shows the Linux command paths. After deploying on
Windows, the workflow's own emitted `next_command` strings are correct
for your install; if the model's FIRST call fails on a Linux-style path,
update the deployed SKILL.md copy to your scripts dir.

## Validate your install

Fetch profiles, then run the two Windows-critical checks:

```bash
python tools/fetch_snapmaker_profiles.py
export ORCA_SLICER_BIN='C:/path/to/orca242/orca-slicer.exe'
python -m pytest tests/test_real_orca_u1_metadata_e2e.py tests/test_u1_lockfile.py -v
```

- The metadata test slices a real cube and asserts the g-code stamps the
  U1 machine AND real PETG values. If it fails with PLA-range values,
  profile inheritance is not resolving — do not print from that install.
- The lockfile test proves the Windows lock backend actually excludes
  concurrent processes (the safety gates depend on it).

Known-good full-suite runs on Windows are the bar for dropping the
"experimental" label.
